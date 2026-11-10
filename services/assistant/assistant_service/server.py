"""The Assistant gRPC service -- the Week 3 skeleton of a chat turn.

What is real already, and stays when Week 5 puts the LangGraph agent behind `_generate`:

- the caller is verified against Part A's JWKS, and refused if their sessions were revoked
  after the token was issued (the deny-list fed by Kafka);
- knowledge is asked through native gRPC with the *caller's* token, so the knowledge
  service applies the caller's own audience (spec section 8.4) -- the assistant never
  searches with authority of its own;
- every event is appended to the turn's Redis Stream before it is sent, so a reconnect
  resumes from `resume_from` and a turn is never generated twice;
- `Cancel` sets a flag the generator checks between tokens; the turn ends with Done.

What is a stand-in: `_generate` echoes the message word by word and cites the hits. No
LLM is called (spec O-9: no key before Week 5).
"""

from __future__ import annotations

import asyncio
import logging

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from smarthealth.assistant.v1 import assistant_pb2, assistant_pb2_grpc
from smarthealth.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc

from assistant_service.auth import Caller, TokenVerifier, Unauthenticated
from assistant_service.stores import Stores

logger = logging.getLogger(__name__)
SERVING = health_pb2.HealthCheckResponse.SERVING
#: Pace of the echo, so streaming is visible in a browser; zero in tests.
TOKEN_DELAY_SECONDS = 0.05


def _to_message(turn_id: str, seq: int, event: dict) -> assistant_pb2.ChatEvent:
    kind = event["kind"]
    if kind == "token":
        return assistant_pb2.ChatEvent(turn_id=turn_id, seq=seq, token=event["text"])
    if kind == "citation":
        return assistant_pb2.ChatEvent(turn_id=turn_id, seq=seq, citation=assistant_pb2.Citation(
            source_id=event["source_id"], title=event["title"]))
    if kind == "error":
        return assistant_pb2.ChatEvent(turn_id=turn_id, seq=seq, error=assistant_pb2.Error(
            code=event["code"], message=event["message"]))
    return assistant_pb2.ChatEvent(turn_id=turn_id, seq=seq,
                                   done=assistant_pb2.Done(reason=event["reason"]))


class AssistantServicer(assistant_pb2_grpc.AssistantServicer):
    def __init__(
        self, stores: Stores, verifier: TokenVerifier,
        knowledge: knowledge_pb2_grpc.KnowledgeStub, *, token_delay: float = TOKEN_DELAY_SECONDS,
    ) -> None:
        self._stores = stores
        self._verifier = verifier
        self._knowledge = knowledge
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

    async def _generate(self, caller: Caller, message: str):
        """The stand-in for the Week 5 agent: search, then echo, then cite."""
        try:
            reply = await self._knowledge.Search(
                knowledge_pb2.SearchRequest(query=message, limit=3),
                metadata=(("authorization", f"Bearer {caller.token}"),), timeout=5,
            )
            hits = list(reply.hits)
        except grpc.aio.AioRpcError as exc:
            # Degrade, do not fail: the turn still answers without knowledge (spec 9.2).
            logger.warning("knowledge unavailable", extra={"code": str(exc.code())})
            hits = []
        for word in ["You", "said:", *message.split()]:
            yield {"kind": "token", "text": word + " "}
        for hit in hits:
            yield {"kind": "citation", "source_id": hit.source_id, "title": hit.title}

    async def Chat(self, request, context):  # noqa: N802 - gRPC method name
        caller = await self._caller(context)
        turn = request.turn_id or "unnamed"
        if await self._stores.exists(turn):
            # A reconnect (or a retry): replay what was recorded, never generate again.
            for seq, event in await self._stores.replay(turn, request.resume_from):
                yield _to_message(turn, seq, event)
            return
        seq = 0
        reason = "complete"
        async for event in self._generate(caller, request.message):
            if await self._stores.cancelled(turn):
                reason = "cancelled"
                break
            seq += 1
            await self._stores.append(turn, seq, event)
            if seq > request.resume_from:
                yield _to_message(turn, seq, event)
            if self._delay:
                await asyncio.sleep(self._delay)
        seq += 1
        done = {"kind": "done", "reason": reason}
        await self._stores.append(turn, seq, done)
        yield _to_message(turn, seq, done)

    async def Cancel(self, request, context):  # noqa: N802
        await self._caller(context)
        return assistant_pb2.CancelReply(cancelled=await self._stores.cancel(request.turn_id))


def build_server(stores: Stores, verifier: TokenVerifier, knowledge_channel: grpc.aio.Channel,
                 port: int, *, token_delay: float = TOKEN_DELAY_SECONDS) -> grpc.aio.Server:
    server = grpc.aio.server()
    servicer = AssistantServicer(
        stores, verifier, knowledge_pb2_grpc.KnowledgeStub(knowledge_channel),
        token_delay=token_delay,
    )
    assistant_pb2_grpc.add_AssistantServicer_to_server(servicer, server)
    checker = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(checker, server)
    server.port = server.add_insecure_port(f"[::]:{port}")  # the bound port (0 picks one)
    server.health = checker
    return server
