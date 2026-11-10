"""Typed messages with no model: route to a flow, or answer from the clinic's documents.

Used when `llm_mode=off`, when a fixture has no recording, and whenever the model fails --
so the assistant degrades to something useful rather than an error (spec 9.2):

1. a message that names a use case ("cancel my appointment", "how do I take my
   prescription") starts that flow -- matched on each flow's keywords, for the caller's
   role only;
2. anything else is searched in knowledge with the caller's audience, and the best passages
   are quoted with their sources. Nothing is paraphrased, because nothing could paraphrase it
   safely without a model.
"""

from __future__ import annotations

import re

from assistant_service.flows import flows_for
from assistant_service.flows.base import Ctx, Flow

_QUESTION = re.compile(
    r"^(is|are|what|whats|what's|how|when|why|where|which|who|can|could|do|"
    r"does|should|will|would)\b|\?\s*$",
    re.IGNORECASE,
)
_PERSONAL = re.compile(r"\b(my|me|mine|i'm|i am)\b", re.IGNORECASE)


def general_question(text: str) -> bool:
    """A question about the clinic, not about the user's own things: the documents answer
    it. "Is there a fee if I cancel late?" is one; "can you cancel my appointment?" is not."""
    return bool(_QUESTION.search(text.strip())) and not _PERSONAL.search(text)


def match_flow(role: str, text: str) -> Flow | None:
    lowered = text.lower().strip(" ?.!")
    for flow in flows_for(role):  # a suggestion chip sends a flow's title verbatim
        if flow.title.lower() == lowered:
            return flow
    if general_question(text):
        return None
    best: tuple[int, Flow] | None = None
    for flow in flows_for(role):
        for keyword in flow.keywords:
            hit = re.search(rf"\b{re.escape(keyword)}\b", lowered)
            if hit and (best is None or len(keyword) > best[0]):
                best = (len(keyword), flow)
    return best[1] if best else None


async def answer_from_documents(ctx: Ctx, text: str, *, note: str = "") -> None:
    hits: list[dict] | None
    try:
        async with ctx.tool("search_knowledge", "Searching the clinic's guides") as step:
            hits = await ctx.search(text, 4)
            step["summary"] = f"{len(hits)} passages"
    except Exception:  # noqa: BLE001 - knowledge down is a degraded answer, not a failure
        hits = None
    if hits is None:
        ctx.paragraph(
            "I can't reach the clinic's guides right now, so I can't answer that. "
            "I can still help with appointments and your record:"
        )
        _offer(ctx)
        return
    if not hits:
        ctx.paragraph(
            "I couldn't find anything about that in the clinic's guides. I can help "
            "with these instead:"
        )
        _offer(ctx)
        return
    if note:
        ctx.paragraph(note)
    ctx.paragraph("Here's what the clinic's guides say:")
    for hit in hits[:2]:
        ctx.paragraph(f"**{hit['title']}** — {hit['snippet']}")
    for hit in hits[:3]:
        ctx.cite(hit["source_id"], hit["title"], hit["snippet"][:240])
    _offer(ctx)


def _offer(ctx: Ctx) -> None:
    ctx.suggest(*[f.title for f in flows_for(ctx.role)][:4])
