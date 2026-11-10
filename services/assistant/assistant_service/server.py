"""The Assistant gRPC service: chat turns, confirmations, cancel, starters.

What every turn gets, whichever way it started (typed, clicked, starter, confirmation):

- the caller verified against Part A's JWKS, and refused if their sessions were revoked
  after the token was issued (the deny-list fed by Kafka);
- knowledge asked through native gRPC with the *caller's* token, so knowledge applies the
  caller's own audience (spec 8.4) -- the assistant never searches with authority of its own;
  Part A called with the caller's token too (parta.py);
- every event appended to the turn's Redis Stream before it is sent, so a reconnect resumes
  from `resume_from` and a turn is never generated twice;
- `Cancel` sets a flag checked between events; the turn ends with Done("cancelled").

The work itself is the LangGraph agent (agent.py).
"""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from langchain_core.language_models.chat_models import BaseChatModel
from smarthealth.assistant.v1 import assistant_pb2, assistant_pb2_grpc
from smarthealth.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc

from assistant_service.agent import Agent, StaleAnswer, TurnContext
from assistant_service.auth import Caller, TokenVerifier, Unauthenticated
from assistant_service.parta import PartA
from assistant_service.starters import starters_for
from assistant_service.stores import Stores

logger = logging.getLogger(__name__)
SERVING = health_pb2.HealthCheckResponse.SERVING
#: Pace of word-by-word prose, so streaming is visible in a browser; zero in tests.
TOKEN_DELAY_SECONDS = 0.02
UTC_ZONE = ZoneInfo("UTC")


def _fields(items: list[dict]) -> list[assistant_pb2.Field]:
    return [assistant_pb2.Field(label=f["label"], value=f["value"]) for f in items]


def to_message(turn_id: str, seq: int, event: dict) -> assistant_pb2.ChatEvent:
    kind = event["kind"]
    base = {"turn_id": turn_id, "seq": seq}
    if kind == "token":
        return assistant_pb2.ChatEvent(**base, token=event["text"])
    if kind == "citation":
        return assistant_pb2.ChatEvent(**base, citation=assistant_pb2.Citation(
            source_id=event["source_id"], title=event["title"], snippet=event.get("snippet", "")))
    if kind == "tool":
        return assistant_pb2.ChatEvent(**base, tool=assistant_pb2.ToolActivity(
            call_id=event["call_id"], name=event["name"], label=event["label"],
            status=event["status"], summary=event.get("summary", "")))
    if kind == "options":
        return assistant_pb2.ChatEvent(**base, options=assistant_pb2.Options(
            options_id=event["id"], prompt=event["prompt"], style=event.get("style", "list"),
            options=[assistant_pb2.Option(id=o["id"], label=o["label"],
                                          description=o.get("description", ""),
                                          badge=o.get("badge", "")) for o in event["options"]]))
    if kind == "confirmation":
        return assistant_pb2.ChatEvent(**base, confirmation=assistant_pb2.ConfirmationRequired(
            confirmation_id=event["id"], action=event["action"], title=event["title"],
            details=_fields(event["details"]), confirm_label=event["confirm_label"],
            deny_label=event["deny_label"], tone=event["tone"]))
    if kind == "card":
        return assistant_pb2.ChatEvent(**base, card=assistant_pb2.Card(
            kind=event["card_kind"], title=event["title"], subtitle=event.get("subtitle", ""),
            fields=_fields(event.get("fields", [])), badge=event.get("badge", ""),
            link=event.get("link", "")))
    if kind == "suggestions":
        return assistant_pb2.ChatEvent(**base, suggestions=assistant_pb2.Suggestions(
            items=event["items"]))
    if kind == "error":
        return assistant_pb2.ChatEvent(**base, error=assistant_pb2.Error(
            code=event["code"], message=event["message"]))
    return assistant_pb2.ChatEvent(**base, done=assistant_pb2.Done(reason=event["reason"]))


class AssistantServicer(assistant_pb2_grpc.AssistantServicer):
    def __init__(
        self, stores: Stores, verifier: TokenVerifier,
        knowledge: knowledge_pb2_grpc.KnowledgeStub, agent: Agent, parta: PartA, *,
        model: BaseChatModel | None, timezone: ZoneInfo,
        token_delay: float = TOKEN_DELAY_SECONDS,
    ) -> None:
        self._stores = stores
        self._verifier = verifier
        self._knowledge = knowledge
        self._agent = agent
        self._parta = parta
        self._model = model
        self._timezone = timezone
        self._delay = token_delay

    async def _caller(self, context) -> Caller:
        metadata = dict(context.invocation_metadata())
        try:
            caller = self._verifier.verify(metadata.get("authorization"))
        except Unauthenticated as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        revoked = await self._stores.revoked_at(caller.user_id)
        if revoked is not None and caller.issued_at <= revoked:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "session revoked")
        return caller

    def _search_as(self, caller: Caller):
        async def search(query: str, limit: int) -> list[dict]:
            reply = await self._knowledge.Search(
                knowledge_pb2.SearchRequest(query=query, limit=limit),
                metadata=(("authorization", f"Bearer {caller.token}"),), timeout=5,
            )
            return [{"source_id": h.source_id, "title": h.title, "snippet": h.snippet,
                     "kind": h.kind, "score": h.score} for h in reply.hits]
        return search

    def _context(self, caller: Caller, conversation_id: str) -> TurnContext:
        return TurnContext(caller=caller, parta=self._parta, search=self._search_as(caller),
                           conversation_id=conversation_id, timezone=self._timezone,
                           model=self._model)

    async def _turn(self, turn: str, resume_from: int, events):
        """Record and send a turn's events; replay instead if the turn already exists."""
        if await self._stores.exists(turn):
            # A reconnect (or a retry): replay what was recorded, never generate again.
            for seq, event in await self._stores.replay(turn, resume_from):
                yield to_message(turn, seq, event)
            return
        seq = 0
        ended = False
        try:
            async for event in events:
                if await self._stores.cancelled(turn):
                    event = {"kind": "done", "reason": "cancelled"}
                seq += 1
                await self._stores.append(turn, seq, event)
                if seq > resume_from:
                    yield to_message(turn, seq, event)
                if event["kind"] == "done":
                    ended = True
                    break
                if self._delay and event["kind"] == "token":
                    await asyncio.sleep(self._delay)
        except StaleAnswer as exc:
            seq += 1
            error = {"kind": "error", "code": "stale", "message": str(exc).capitalize() + "."}
            await self._stores.append(turn, seq, error)
            yield to_message(turn, seq, error)
        except Exception:
            logger.exception("turn failed", extra={"turn_id": turn})
            seq += 1
            error = {"kind": "error", "code": "internal",
                     "message": "Something went wrong on my side. Please try again."}
            await self._stores.append(turn, seq, error)
            yield to_message(turn, seq, error)
        if not ended:
            seq += 1
            done = {"kind": "done", "reason": "complete"}
            await self._stores.append(turn, seq, done)
            yield to_message(turn, seq, done)

    async def Chat(self, request, context):  # noqa: N802 - gRPC method name
        caller = await self._caller(context)
        turn = request.turn_id or "unnamed"
        choice = ((request.choice.options_id, request.choice.option_id)
                  if request.HasField("choice") else None)
        events = self._agent.run(self._context(caller, request.conversation_id or turn),
                                 text=request.message, starter=request.starter_id, choice=choice)
        async for message in self._turn(turn, request.resume_from, events):
            yield message

    async def Confirm(self, request, context):  # noqa: N802
        caller = await self._caller(context)
        turn = request.turn_id or "unnamed"
        events = self._agent.run(self._context(caller, request.conversation_id),
                                 decision=(request.confirmation_id, request.approved))
        async for message in self._turn(turn, request.resume_from, events):
            yield message

    async def Cancel(self, request, context):  # noqa: N802
        await self._caller(context)
        return assistant_pb2.CancelReply(cancelled=await self._stores.cancel(request.turn_id))

    async def Starters(self, request, context):  # noqa: N802
        caller = await self._caller(context)
        return assistant_pb2.StartersReply(groups=[
            assistant_pb2.StarterGroup(title=title, starters=[
                assistant_pb2.Starter(id=s.id, title=s.title, description=s.description,
                                      icon=s.icon, mode=s.mode, prompt=s.prompt)
                for s in starters])
            for title, starters in starters_for(caller.role)])


def build_server(stores: Stores, verifier: TokenVerifier, knowledge_channel: grpc.aio.Channel,
                 port: int, *, agent: Agent, parta: PartA, model: BaseChatModel | None,
                 timezone: ZoneInfo = UTC_ZONE,
                 token_delay: float = TOKEN_DELAY_SECONDS) -> grpc.aio.Server:
    server = grpc.aio.server()
    servicer = AssistantServicer(
        stores, verifier, knowledge_pb2_grpc.KnowledgeStub(knowledge_channel), agent, parta,
        model=model, timezone=timezone, token_delay=token_delay,
    )
    assistant_pb2_grpc.add_AssistantServicer_to_server(servicer, server)
    checker = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(checker, server)
    server.port = server.add_insecure_port(f"[::]:{port}")  # the bound port (0 picks one)
    server.health = checker
    return server
