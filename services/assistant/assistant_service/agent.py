"""The assistant's agent: one LangGraph graph per conversation (spec 9.2).

```
            ┌──────── starter / handoff ────────┐
 START → begin ──text──→ chat ──start_flow──→ flow ⇄ ask (interrupt: options / confirm / text)
                           │                    │
                           └──answer──→ END ←───┘ finished
```

- **begin** reads the turn's input: a starter goes straight to its flow; text goes to chat.
- **chat** is the typed-message brain. Red flags are answered first and deterministically
  (guard.py). Then the model -- if there is one -- answers knowledge questions with the
  `search_knowledge` tool and hands every action to a flow with `start_flow`; **the model
  never writes anything itself**, so every change meets the same human confirmation. With
  no model, or when it fails, offline.py routes or quotes the documents.
- **flow** runs one step of the active guided flow (flows/), which either finishes or asks.
- **ask** pauses the graph with a LangGraph `interrupt`. The conversation's checkpoint holds
  the flow's memory, so the answer -- a click, a typed reply, a yes or no -- may come in a
  later request, to a different replica, after a restart, and resumes the same flow.

Events reach the client through LangGraph's custom stream (`get_stream_writer`); the
interrupt's payload becomes the Options / ConfirmationRequired event. Each thread is keyed
by user *and* conversation id, so one user can never resume another's conversation.
"""

from __future__ import annotations

import copy
import re
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from assistant_service.auth import Caller
from assistant_service.flows import FLOWS, flows_for
from assistant_service.flows.base import Ctx, DraftLog, FlowError, NotAnAnswer, Search
from assistant_service.guard import guidance, red_flag
from assistant_service.llm import LLMUnavailable
from assistant_service.offline import answer_from_documents, match_flow
from assistant_service.parta import PartA, PartAError
from assistant_service.usage import TurnUsage

logger = logging.getLogger(__name__)
HISTORY = 12
MAX_TOOL_ROUNDS = 4


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    input: dict
    flow: str | None
    data: dict
    ask: dict | None
    answer: dict | None
    #: Set when a flow gave typed text back: chat handles it as a new request.
    redirect: bool


@dataclass
class TurnContext:
    """Per-run dependencies, passed as the graph's runtime context (never checkpointed)."""

    caller: Caller
    parta: PartA
    search: Search
    conversation_id: str
    timezone: ZoneInfo
    model: BaseChatModel | None
    #: The turn's model calls and tokens, read by the server when the turn ends.
    usage: TurnUsage = field(default_factory=TurnUsage)
    drafts: DraftLog | None = None

    async def write(self, instructions: str, material: str) -> str | None:
        """One drafting call to the model; None without a model or on any failure."""
        if self.model is None:
            return None
        try:
            reply = await self.model.ainvoke([SystemMessage(instructions), HumanMessage(material)])
        except Exception:  # noqa: BLE001 - a draft falls back to the template
            logger.info("drafting with the model failed; using the template", exc_info=True)
            return None
        self.usage.add_call(getattr(reply, "usage_metadata", None))
        text = _text(reply).strip()
        return text or None

    def ctx(self) -> Ctx:
        return Ctx(
            caller=self.caller,
            parta=self.parta,
            search=self.search,
            emit=get_stream_writer(),
            write=self.write,
            drafts=self.drafts,
            conversation_id=self.conversation_id,
            timezone=self.timezone,
        )


# -- nodes -------------------------------------------------------------------------------


async def begin(state: State, runtime: Runtime[TurnContext]) -> dict:
    starter = state["input"].get("starter")
    reset = {"data": {}, "ask": None, "answer": None, "redirect": False}
    return {"flow": starter or None, **reset}


def after_begin(state: State) -> str:
    return "flow" if state.get("flow") else "chat"


async def flow_node(state: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context.ctx()
    flow = FLOWS.get(state.get("flow") or "")
    if flow is None or ctx.role not in flow.roles:
        ctx.paragraph("That isn't something I can help you with.")
        return {"flow": None, "ask": None, "answer": None}
    data = copy.deepcopy(state.get("data") or {})
    try:
        asked = await flow.step(ctx, data, state.get("answer"))
    except NotAnAnswer as exc:
        return {
            "flow": None,
            "data": {},
            "ask": None,
            "answer": None,
            "redirect": True,
            "input": {"text": exc.text, "starter": ""},
        }
    except FlowError as exc:
        ctx.paragraph(str(exc))
        return {"flow": None, "data": {}, "ask": None, "answer": None}
    except PartAError as exc:
        ctx.paragraph(f"The booking system refused that: {exc.detail}")
        return {"flow": None, "data": {}, "ask": None, "answer": None}
    if asked is None:
        return {"flow": None, "data": {}, "ask": None, "answer": None}
    return {"data": data, "ask": asked.payload(), "answer": None}


def after_flow(state: State) -> str:
    if state.get("redirect"):
        return "chat"
    return "ask" if state.get("ask") else END


async def ask_node(state: State) -> dict:
    # Nothing before interrupt() may have effects: on resume this node runs again from the
    # top, and the value the user sent is returned here.
    answer = interrupt(state["ask"])
    return {"answer": answer, "ask": None}


class _StartFlow(BaseModel):
    flow_id: str = Field(description="Which guided flow to start")
    specialty: str | None = Field(default=None, description="A specialty the user named, if any")
    doctor: str | None = Field(default=None, description="A doctor the user named, if any")
    message: str | None = Field(
        default=None,
        description="For a message to patients: what the user wants them told, in their words",
    )


class _Search(BaseModel):
    query: str = Field(description="What to look up in the clinic's guides")


def system_prompt(ctx: Ctx) -> str:
    flows = "\n".join(f"- {f.id}: {f.title} — {f.description}" for f in flows_for(ctx.role))
    return (
        "You are MediNova's assistant. You speak to a "
        f"{ctx.role.replace('_', ' ')}. Be brief, warm and plain-spoken.\n"
        "Rules you never break:\n"
        "- Never diagnose, never suggest a treatment, a medicine, a dose or a test.\n"
        "- Answer questions about the clinic only from search_knowledge results, and say so "
        "when they do not cover the question.\n"
        "- You cannot change anything yourself. For any action or any question about the "
        "user's own appointments, record, prescriptions or results, call start_flow; it shows "
        "the user the real data and asks them to confirm changes.\n"
        "- Every number you state must come from a tool result.\n"
        "- Never write source ids, brackets or citation marks: the sources you used are "
        "shown to the user beside your answer.\n"
        f"Flows you may start:\n{flows}"
    )


async def _model_turn(
    ctx: Ctx, model: BaseChatModel, history: list[AnyMessage], text: str, usage: TurnUsage
) -> tuple[str, dict | None]:
    """The model's answer, streamed; returns (answer text, handoff or None)."""
    allowed = {f.id for f in flows_for(ctx.role)}
    retrieved: list[dict] = []  # every passage searched this turn; cited only if used

    async def search_knowledge(query: str) -> str:
        return ""  # executed below, where events can be emitted

    async def start_flow(
        flow_id: str, specialty: str | None = None, doctor: str | None = None
    ) -> str:
        return ""

    tools = [
        StructuredTool.from_function(
            coroutine=search_knowledge,
            name="search_knowledge",
            description="Search the clinic's own guides",
            args_schema=_Search,
        ),
        StructuredTool.from_function(
            coroutine=start_flow,
            name="start_flow",
            description="Start a guided flow for the user",
            args_schema=_StartFlow,
        ),
    ]
    bound = model.bind_tools(tools)
    messages: list[AnyMessage] = [SystemMessage(system_prompt(ctx)), *history, HumanMessage(text)]
    for _ in range(MAX_TOOL_ROUNDS):
        reply: AIMessage | None = None
        async for chunk in bound.astream(messages):
            if isinstance(chunk.content, str) and chunk.content:
                usage.first_token()
                ctx.emit({"kind": "token", "text": chunk.content})
            reply = chunk if reply is None else reply + chunk
        if reply is None:
            raise LLMUnavailable("empty response")
        usage.add_call(getattr(reply, "usage_metadata", None))
        messages.append(reply)
        if not reply.tool_calls:
            answer = _text(reply)
            for hit in sources_used(answer, retrieved):
                ctx.cite(hit["source_id"], hit["title"], hit["snippet"][:240])
            return answer, None
        for call in reply.tool_calls:
            if call["name"] == "start_flow" and call["args"].get("flow_id") in allowed:
                prefill = {
                    k: v
                    for k, v in (
                        ("specialty", call["args"].get("specialty")),
                        ("provider_hint", call["args"].get("doctor")),
                        ("brief_hint", call["args"].get("message")),
                    )
                    if v
                }
                return _text(reply), {"flow": call["args"]["flow_id"], "data": prefill}
            if call["name"] == "search_knowledge":
                async with ctx.tool("search_knowledge", "Searching the clinic's guides") as step:
                    hits = await ctx.search(call["args"]["query"], 4)
                    step["summary"] = f"{len(hits)} passages"
                retrieved.extend(hits)
                content = (
                    "\n\n".join(f"[{h['source_id']}] {h['title']}: {h['snippet']}" for h in hits)
                    or "No relevant passages."
                )
            else:
                content = "That flow is not available to this user."
            messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
    return "", None


_STOP = frozenset(
    """about after again also before been being below book could does doing done during
each every first from have having help here into just like make more most need only other
over same should since some such than that their them then there these they this those
through time under until very want what when where which while will with would your yours
please thank thanks clinic medinova appointment""".split()
)


def _terms(text: str) -> set[str]:
    """Distinctive words (four letters or more, common ones dropped), by their first six
    letters (so "cancel" meets "cancelling", "knee" meets "knee")."""
    return {w[:6] for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


def sources_used(answer: str, hits: list[dict], *, limit: int = 3) -> list[dict]:
    """The passages an answer actually draws on: at least two distinctive words shared.

    A search returns passages that merely mention a query word -- knee pain fetches the
    chest-pain guide, both say "pain". Citing everything retrieved would show the user a
    source the answer never used (the offline judge caught exactly this, ai-001); citing
    only what the answer shares words with keeps every source honest.
    """
    words = _terms(answer)
    used, seen = [], set()
    for hit in hits:
        if hit["source_id"] in seen:
            continue
        if len(words & _terms(hit["title"] + " " + hit["snippet"])) >= 2:
            used.append(hit)
            seen.add(hit["source_id"])
    return used[:limit]


def _text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(part.get("text", "") for part in message.content if isinstance(part, dict))


async def chat_node(state: State, runtime: Runtime[TurnContext]) -> dict:
    # Clears a flow's redirect, so a handoff from here into a flow is not sent back.
    return {**(await _chat(state, runtime)), "redirect": False}


async def _chat(state: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context.ctx()
    text = state["input"].get("text", "").strip()
    human = HumanMessage(text)
    if not text:
        ctx.paragraph("What can I help with?")
        ctx.suggest(*[f.title for f in flows_for(ctx.role)][:4])
        return {}
    flag = red_flag(text)
    if flag:
        ctx.paragraph(guidance(flag))
        ctx.cite("doc:urgent-care", "When to seek urgent care")
        return {"messages": [human, AIMessage(guidance(flag))]}
    model = runtime.context.model
    if model is not None:
        history = list(state.get("messages") or [])[-HISTORY:]
        try:
            answer, handoff = await _model_turn(ctx, model, history, text, runtime.context.usage)
        except LLMUnavailable as exc:
            logger.info("model unavailable, answering offline", extra={"reason": str(exc)})
        except Exception:  # noqa: BLE001 - a provider failure degrades the turn, never fails it
            logger.exception("model call failed, answering offline")
        else:
            update: dict[str, Any] = {"messages": [human, AIMessage(answer or "(started a flow)")]}
            if handoff:
                update.update(flow=handoff["flow"], data=handoff["data"], ask=None, answer=None)
            return update
    flow = match_flow(ctx.role, text)
    if flow is not None:
        return {"messages": [human], "flow": flow.id, "data": {}, "ask": None, "answer": None}
    await answer_from_documents(ctx, text)
    return {"messages": [human]}


def after_chat(state: State) -> str:
    return "flow" if state.get("flow") else END


def build_graph(checkpointer: BaseCheckpointSaver):
    graph = StateGraph(State, context_schema=TurnContext)
    graph.add_node("begin", begin)
    graph.add_node("chat", chat_node)
    graph.add_node("flow", flow_node)
    graph.add_node("ask", ask_node)
    graph.add_edge(START, "begin")
    graph.add_conditional_edges("begin", after_begin, ["flow", "chat"])
    graph.add_conditional_edges("chat", after_chat, ["flow", END])
    graph.add_conditional_edges("flow", after_flow, ["ask", "chat", END])
    graph.add_edge("ask", "flow")
    return graph.compile(checkpointer=checkpointer)


# -- running a turn ----------------------------------------------------------------------


class StaleAnswer(Exception):
    """The answer names a question that is no longer the one waiting."""


class Agent:
    def __init__(self, checkpointer: BaseCheckpointSaver) -> None:
        self.graph = build_graph(checkpointer)

    @staticmethod
    def thread(caller: Caller, conversation_id: str) -> dict:
        return {"configurable": {"thread_id": f"{caller.user_id}:{conversation_id}"}}

    async def pending(self, caller: Caller, conversation_id: str) -> dict | None:
        snapshot = await self.graph.aget_state(self.thread(caller, conversation_id))
        for task in snapshot.tasks:
            for pending in task.interrupts:
                return pending.value
        return None

    async def run(
        self,
        context: TurnContext,
        *,
        text: str = "",
        starter: str = "",
        choice: tuple[str, str] | None = None,
        decision: tuple[str, bool] | None = None,
    ) -> AsyncIterator[dict]:
        """Run one turn, yielding client events, and finally a `done` event."""
        config = self.thread(context.caller, context.conversation_id)
        waiting = await self.pending(context.caller, context.conversation_id)
        if choice is not None:
            if not waiting or waiting.get("type") != "options" or waiting["id"] != choice[0]:
                raise StaleAnswer("that choice is no longer open")
            command: Any = Command(resume={"option_id": choice[1]})
        elif decision is not None:
            if not waiting or waiting.get("type") != "confirmation" or waiting["id"] != decision[0]:
                raise StaleAnswer("that confirmation is no longer open")
            command = Command(resume={"approved": decision[1]})
        elif (
            text and not starter and waiting and waiting.get("accepts_text") and not red_flag(text)
        ):
            command = Command(resume={"text": text})
        else:
            # A new request: anything still waiting is abandoned.
            command = {"input": {"text": text, "starter": starter}}
        paused: dict | None = None
        async for mode, chunk in self.graph.astream(
            command, config, context=context, stream_mode=["custom", "updates"]
        ):
            if mode == "custom":
                yield chunk
            elif "__interrupt__" in chunk:
                paused = chunk["__interrupt__"][0].value
        if paused is not None:
            yield ask_event(paused)
            yield {"kind": "done", "reason": "awaiting_input"}
        else:
            yield {"kind": "done", "reason": "complete"}


def ask_event(payload: dict) -> dict:
    if payload["type"] == "options":
        return {"kind": "options", **{k: payload[k] for k in ("id", "prompt", "style", "options")}}
    if payload["type"] == "confirmation":
        return {"kind": "confirmation", **{k: v for k, v in payload.items() if k != "type"}}
    return {"kind": "token", "text": payload["prompt"]}
