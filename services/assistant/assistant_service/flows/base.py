"""Guided flows: the click-driven half of the assistant.

A flow is one use case from spec 9.1 (book, reschedule, prepare for a test, brief me on my
next patient ...) written as a small state machine. Each `step` reads the flow's memory
(`data`, a JSON-able dict kept in the conversation's checkpoint) and the user's latest
answer, does its lookups through Part A *with the user's token*, says what it found, and
either asks the next question or finishes:

- `AskOptions` -- a choice the user clicks (or types: `accepts_text`);
- `AskConfirm` -- a write the assistant will make only on the user's yes (spec 9.2);
- `AskText` -- a free answer ("which patient? MRN or name").

The graph (agent.py) pauses on every ask with a LangGraph interrupt, and resumes the same
flow with the answer. **Flows need no LLM**, so every guided use case works with no model
at all -- and the model, when there is one, hands off into a flow instead of writing
anything itself (agent.py: `start_flow`). That keeps every write behind the same
confirmation, whoever started it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from assistant_service.auth import Caller
from assistant_service.parta import PartA, PartAError, PartAUnavailable

Emit = Callable[[dict], None]
Search = Callable[[str, int], Awaitable[list[dict]]]
#: (instructions, material) -> the model's text, or None.
Write = Callable[[str, str], Awaitable[str | None]]
#: (draft id, kind, status, recipients) -> recorded.
DraftLog = Callable[[str, str, str, int], Awaitable[None]]


@dataclass
class Choice:
    id: str
    label: str
    description: str = ""
    badge: str = ""


@dataclass
class AskOptions:
    prompt: str
    options: list[Choice]
    style: str = "list"
    #: The user may type instead of clicking; the text arrives as {"text": ...}.
    accepts_text: bool = False

    def payload(self) -> dict:
        return {
            "type": "options",
            "id": f"opt-{uuid.uuid4().hex[:12]}",
            "prompt": self.prompt,
            "style": self.style,
            "accepts_text": self.accepts_text,
            "options": [c.__dict__ for c in self.options],
        }


@dataclass
class AskConfirm:
    action: str
    title: str
    details: list[tuple[str, str]]
    confirm_label: str = "Confirm"
    deny_label: str = "Not now"
    tone: str = "normal"

    def payload(self) -> dict:
        return {
            "type": "confirmation",
            "id": f"cnf-{uuid.uuid4().hex[:12]}",
            "action": self.action,
            "title": self.title,
            "details": [{"label": k, "value": v} for k, v in self.details],
            "confirm_label": self.confirm_label,
            "deny_label": self.deny_label,
            "tone": self.tone,
        }


@dataclass
class AskText:
    prompt: str

    def payload(self) -> dict:
        return {
            "type": "text",
            "id": f"txt-{uuid.uuid4().hex[:12]}",
            "prompt": self.prompt,
            "accepts_text": True,
        }


Ask = AskOptions | AskConfirm | AskText


class FlowError(Exception):
    """The flow cannot go on; its message is said to the user as is."""


class NotAnAnswer(Exception):
    """Typed text that does not answer the open question: the flow is dropped and the text
    is handled as a new request (agent.py), so "actually, how do I take my pills?" in the
    middle of a booking gets an answer, not the same question again."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass
class Ctx:
    """What a step may use. Built fresh for every run; never checkpointed."""

    caller: Caller
    parta: PartA
    search: Search
    emit: Emit
    conversation_id: str
    timezone: ZoneInfo
    #: Drafting with the model (generation use cases, spec 9.1): returns the text, or None
    #: when there is no model or it failed -- the flow then falls back to a template.
    write: Write | None = None
    #: Where drafts are counted (generated, sent, discarded) for PART-B-FR-5.
    drafts: DraftLog | None = None
    _calls: int = field(default=0)

    @property
    def token(self) -> str:
        return self.caller.token

    @property
    def role(self) -> str:
        return self.caller.role

    # -- what the user sees -----------------------------------------------------------

    def say(self, text: str) -> None:
        # Words, not characters: a flow's prose arrives at reading pace without a token
        # event per letter.
        words = text.split(" ")
        for index, word in enumerate(words):
            self.emit({"kind": "token", "text": word + (" " if index < len(words) - 1 else "")})

    def paragraph(self, text: str) -> None:
        self.say(text.rstrip() + "\n\n")

    def card(
        self,
        kind: str,
        title: str,
        *,
        subtitle: str = "",
        fields: list[tuple[str, str]] | None = None,
        badge: str = "",
        link: str = "",
    ) -> None:
        self.emit(
            {
                "kind": "card",
                "card_kind": kind,
                "title": title,
                "subtitle": subtitle,
                "fields": [{"label": k, "value": v} for k, v in (fields or []) if v],
                "badge": badge,
                "link": link,
            }
        )

    def cite(self, source_id: str, title: str, snippet: str = "") -> None:
        self.emit({"kind": "citation", "source_id": source_id, "title": title, "snippet": snippet})

    def suggest(self, *items: str) -> None:
        self.emit({"kind": "suggestions", "items": list(items)})

    @asynccontextmanager
    async def tool(self, name: str, label: str):
        """One step taken on the user's behalf, shown running and then done or failed."""
        self._calls += 1
        call_id = f"{name}-{self._calls}-{uuid.uuid4().hex[:6]}"
        step = {"summary": ""}
        self.emit(
            {
                "kind": "tool",
                "call_id": call_id,
                "name": name,
                "label": label,
                "status": "running",
                "summary": "",
            }
        )
        try:
            yield step
        except PartAUnavailable:
            self.emit(
                {
                    "kind": "tool",
                    "call_id": call_id,
                    "name": name,
                    "label": label,
                    "status": "failed",
                    "summary": "The booking system is not responding",
                }
            )
            raise FlowError(
                "I can't reach the booking system right now, so I can't check "
                "that. Please try again in a minute."
            ) from None
        except PartAError as exc:
            self.emit(
                {
                    "kind": "tool",
                    "call_id": call_id,
                    "name": name,
                    "label": label,
                    "status": "failed",
                    "summary": exc.detail[:200],
                }
            )
            raise
        except Exception:
            self.emit(
                {
                    "kind": "tool",
                    "call_id": call_id,
                    "name": name,
                    "label": label,
                    "status": "failed",
                    "summary": "",
                }
            )
            raise
        self.emit(
            {
                "kind": "tool",
                "call_id": call_id,
                "name": name,
                "label": label,
                "status": "done",
                "summary": step["summary"],
            }
        )

    # -- formatting ---------------------------------------------------------------------

    def when(self, iso: str | None) -> str:
        if not iso:
            return "no time set"
        moment = datetime.fromisoformat(iso).astimezone(self.timezone)
        return moment.strftime("%a %d %b, %H:%M")

    def day(self, iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(self.timezone).strftime("%a %d %b")


class Flow:
    """One guided use case. Subclasses set the class attributes and write `step`."""

    id: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    icon: ClassVar[str]
    roles: ClassVar[frozenset[str]]
    group: ClassVar[str]
    #: Words that send a typed request here when no model is available (offline.py).
    keywords: ClassVar[tuple[str, ...]] = ()

    async def step(self, ctx: Ctx, data: dict[str, Any], answer: dict | None) -> Ask | None:
        raise NotImplementedError


def option_id(answer: dict | None) -> str | None:
    return (answer or {}).get("option_id")


def typed(answer: dict | None) -> str | None:
    text = (answer or {}).get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def doctor(provider: dict) -> str:
    return f"Dr {provider['first_name']} {provider['last_name']}"
