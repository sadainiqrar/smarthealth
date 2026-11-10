"""The `ai` step kind: a turn with the assistant, inside a declarative case.

    steps:
      - ai: { as: patient, say: "I've had knee pain for two weeks. Who should I see?",
              expect: { cites: true, never_says: ["diagnos"] } }
      - ai: { pick: "First available" }          # answer the last choice by its label
      - ai: { confirm: true, expect: { writes: 1 } }

Each case talks to the real assistant agent in-process -- the LangGraph graph, the flows, the
guard -- against the contract tier's fake Part A and a fake knowledge service holding
`CLINIC_GUIDES`. The model is the recorded one (`services/assistant/eval/fixtures/llm.jsonl`,
replayed; `ASSISTANT_GOLDEN=record` with a key records it), so a case is deterministic and
offline. **Structural** expectations are asserted here, in pytest; the **semantic** question
in the case's `judge` block is left to the offline judge, which reads the transcript this
module writes to `tests/reports/ai-run.json` (harness spec section 11). A judge verdict
never changes this test's result.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tests.harness.partb  # noqa: F401, I001 - the services' import paths
import assistant_service  # noqa: F401, E402, I001 - puts the generated bindings on the path
from smarthealth.knowledge.v1 import knowledge_pb2  # noqa: E402, I001

REPORT = Path(__file__).resolve().parents[1] / "reports" / "ai-run.json"
ROLES = {"patient": "u-patient", "provider": "u-ada", "front_desk": "u-desk", "admin": "u-admin"}


def _hit(source_id: str, title: str, snippet: str) -> knowledge_pb2.Hit:
    return knowledge_pb2.Hit(
        source_id=source_id, kind="document", title=title, snippet=snippet, score=0.9
    )


#: The clinic's guides as the `ai` cases see them: short, real-shaped passages from the
#: corpus (services/knowledge/corpus), so groundedness can be judged against them.
CLINIC_GUIDES = [
    _hit(
        "doc:specialties#orthopaedics",
        "Orthopaedics",
        "Orthopaedics treats bones, joints and muscles: knee, hip, back and shoulder pain, "
        "sprains and sports injuries. For joint pain lasting more than two weeks, book "
        "Orthopaedics; for a sudden injury you cannot put weight on, use urgent care.",
    ),
    _hit(
        "doc:specialties#physiotherapy",
        "Physiotherapy",
        "Physiotherapy helps recovery after an injury or surgery. A referral from your "
        "clinician is needed for physiotherapy at MediNova.",
    ),
    _hit(
        "doc:prep-imaging#mri-scan",
        "MRI scan",
        "Before an MRI remove all metal: jewellery, watches, hair clips and clothes with "
        "zips. Tell us beforehand about a pacemaker, implants or metal fragments. You can eat "
        "and drink as normal. The scan takes 30 to 60 minutes.",
    ),
    _hit(
        "doc:policies#late-cancellation",
        "Late cancellation",
        "Cancelling less than 24 hours before your appointment costs a fee of 20.",
    ),
    _hit(
        "doc:urgent-care#chest-pain",
        "Chest pain",
        "Chest pain or pressure that spreads to the arm, jaw or back may be a heart attack. "
        "Call the emergency number immediately.",
    ),
]


class Session:
    """One case's conversation: the harness, the model, and the transcript."""

    def __init__(self, case) -> None:
        self.case = case
        self.transcript: list[dict[str, Any]] = []
        self._harness = None
        self.model = None
        self.last_events: list = []

    async def open(self) -> None:
        from tests.harness.fake_part_a import FakePartA
        from tests.tiers.t1_contract.test_assistant_agent import Harness
        from tests.tiers.t1_contract.test_assistant_golden import _model

        self.model = _model()
        self.fake = FakePartA.seeded()
        self._harness = Harness(self.fake, model=self.model, passages=CLINIC_GUIDES)
        await self._harness.__aenter__()

    async def close(self) -> None:
        if self._harness is not None:
            await self._harness.__aexit__(None, None, None)
        _write_row(self)

    @property
    def h(self):
        return self._harness


def _summary(events) -> dict[str, Any]:
    """The turn as a reader sees it: prose, and every interactive part in words."""
    kinds = [e.WhichOneof("kind") for e in events]
    reply = "".join(e.token for e in events if e.WhichOneof("kind") == "token").strip()
    out: dict[str, Any] = {"reply": reply, "kinds": sorted(set(kinds))}
    for e in events:
        kind = e.WhichOneof("kind")
        if kind == "options":
            out.setdefault("options", []).append(
                {"prompt": e.options.prompt, "choices": [o.label for o in e.options.options]}
            )
        elif kind == "confirmation":
            out["confirmation"] = {
                "title": e.confirmation.title,
                "details": {d.label: d.value for d in e.confirmation.details},
            }
        elif kind == "card":
            out.setdefault("cards", []).append(
                {
                    "title": e.card.title,
                    "badge": e.card.badge,
                    "fields": {f.label: f.value for f in e.card.fields},
                }
            )
        elif kind == "citation":
            out.setdefault("cited", []).append(e.citation.source_id)
        elif kind == "error":
            out["error"] = e.error.message
    return out


async def run_ai_step(step, ctx, where: str) -> None:
    """The `ai` step handler (tests/runner/engine.py)."""
    from tests.tiers.t1_contract.test_assistant_agent import _token
    from tests.tiers.t1_contract.test_assistant_golden import _replayed

    from app.modules.identity.models import UserRole

    spec: dict[str, Any] = dict(step.ai)
    session: Session | None = ctx.notes.get("ai")
    if session is None:
        session = ctx.notes["ai"] = Session(ctx.notes["case"])
        await session.open()
        session.role = spec.get("as", "patient")
    role = spec.get("as", session.role)
    token = _token(UserRole(role), user=ROLES[role])
    conversation = ctx.notes["case"].id
    h = session.h
    if "say" in spec:
        sent = spec["say"]
        events = await h.chat(token, conversation=conversation, message=sent)
    elif "pick" in spec:
        options = [e.options for e in session.last_events if e.WhichOneof("kind") == "options"]
        assert options, f"{where}: `pick` needs the previous turn to offer choices"
        wanted = spec["pick"].lower()
        match = [o for o in options[-1].options if o.label.lower().startswith(wanted)]
        assert match, (
            f"{where}: no choice starts with {spec['pick']!r}: "
            f"{[o.label for o in options[-1].options]}"
        )
        sent = f"[picked] {match[0].label}"
        events = await h.chat(
            token, conversation=conversation, choice=(options[-1].options_id, match[0].id)
        )
    elif "confirm" in spec:
        pending = [
            e.confirmation for e in session.last_events if e.WhichOneof("kind") == "confirmation"
        ]
        assert pending, f"{where}: `confirm` needs the previous turn to ask for a confirmation"
        approved = bool(spec["confirm"])
        sent = "[confirmed]" if approved else "[declined]"
        events = await h.confirm(
            token, pending[-1].confirmation_id, approved, conversation=conversation
        )
    else:
        raise AssertionError(f"{where}: an `ai` step needs one of say, pick or confirm")
    _replayed(session.model)
    session.last_events = events
    turn = {"sent": sent, **_summary(events)}
    session.transcript.append(turn)
    _check(spec.get("expect") or {}, turn, session, where)


def _check(expect: dict[str, Any], turn: dict[str, Any], session: Session, where: str) -> None:
    text = (
        (
            turn["reply"]
            + " "
            + json.dumps(
                {k: v for k, v in turn.items() if k in ("options", "cards", "confirmation")}
            )
        )
        .lower()
        .replace("’", "'")
    )
    if (cites := expect.get("cites")) is not None:
        cited = turn.get("cited", [])
        if cites is True:
            assert cited, f"{where}: expected a cited source; reply was {turn['reply'][:200]!r}"
        else:
            assert any(c.startswith(cites) for c in cited), (
                f"{where}: expected {cites}, got {cited}"
            )
    for phrase in expect.get("says", []):
        assert phrase.lower() in text, f"{where}: expected {phrase!r} in {turn['reply'][:300]!r}"
    for phrase in expect.get("never_says", []):
        assert phrase.lower() not in text, (
            f"{where}: {phrase!r} must not appear: {turn['reply'][:300]!r}"
        )
    if (offers := expect.get("offers")) is not None:
        assert ("options" in turn["kinds"]) == bool(offers), (
            f"{where}: offers={offers} but kinds={turn['kinds']}"
        )
    if (asks := expect.get("asks_to_confirm")) is not None:
        assert ("confirmation" in turn["kinds"]) == bool(asks), f"{where}: asks_to_confirm={asks}"
    if (writes := expect.get("writes")) is not None:
        assert len(session.fake.writes) == writes, (
            f"{where}: expected {writes} write(s) to Part A, saw {len(session.fake.writes)}"
        )


def _write_row(session: Session) -> None:
    """Merge this case's transcript into tests/reports/ai-run.json (git-ignored)."""
    case = session.case
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = json.loads(REPORT.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        report = {"cases": {}}
    retrieved = []
    for hit in session.h.knowledge.returned if session.h else []:
        item = {"source_id": hit.source_id, "title": hit.title, "snippet": hit.snippet}
        if item not in retrieved:
            retrieved.append(item)
    report["cases"][case.id] = {
        "id": case.id,
        "title": case.title,
        "judge_prompt": case.judge.prompt if case.judge else None,
        "rubrics": list(case.judge.rubrics) if case.judge else [],
        "transcript": session.transcript,
        "retrieved": retrieved,
        "reply_preview": (session.transcript[-1]["reply"][:300] if session.transcript else ""),
        "model": "recorded" if os.environ.get("ASSISTANT_GOLDEN") != "record" else "live",
        "ran_at": datetime.now(UTC).isoformat(),
    }
    report["written_at"] = datetime.now(UTC).isoformat()
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


async def close(ctx) -> None:
    session: Session | None = ctx.notes.pop("ai", None)
    if session is not None:
        await session.close()
