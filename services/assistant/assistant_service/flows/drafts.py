"""Drafts a person approves (spec 9.1, "Generation"; PART-B-FR-3).

The assistant writes; a person edits and approves; Part A acts. Two use cases:

- **Message a doctor's patients** (front desk, admin): "Dr Y is away next week -- tell her
  patients". The flow finds the patients booked with that doctor in the window, drafts one
  notice, lets the sender edit it, and only on a confirmed "send" asks Part A's
  `POST /notifications` once per patient (an Idempotency-Key per draft and patient, so a
  retried approval sends nothing twice). Delivery, throttling and audit are Part A's.
- **Write patient instructions** (provider): turns the clinician's own draft note into a
  plain-language patient summary, which the doctor edits and saves into the note -- the note
  stays a draft the doctor signs (spec 9.6). The model is told to add nothing: no diagnosis,
  no medicine, no test the note does not already contain.

With no model (or when it fails) both fall back to a plain template, so the flows work
offline. Each draft's life -- generated, sent or saved, discarded -- is counted for
PART-B-FR-5 ("generated communication usage").
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from assistant_service.flows.base import (
    AskConfirm,
    AskOptions,
    AskText,
    Choice,
    Ctx,
    FlowError,
    doctor,
    option_id,
    typed,
)
from assistant_service.flows.record import my_provider_id
from assistant_service.flows.staged import StagedFlow
from assistant_service.parta import PartAError

NOTICE_INSTRUCTIONS = (
    "You write short notices from MediNova clinic to its patients. Plain text, warm and "
    "clear, under 110 words. No clinical content of any kind. Do not invent dates, times, "
    "names or offers beyond what you are given. Do not write a greeting or a sign-off: they "
    "are added. Reply with the subject on the first line, then a blank line, then the body."
)
INSTRUCTIONS_INSTRUCTIONS = (
    "You turn a clinician's visit note into instructions for the patient, in plain "
    "language a non-medical reader understands, under 180 words, in short paragraphs or a "
    "short list. Use only what the note says: never add a diagnosis, a medicine, a dose, a "
    "test or advice the note does not contain, and never change a dose. Leave out the "
    "clinician-only remarks. Reply with the instructions only."
)


async def _log(ctx: Ctx, data: dict, status: str, recipients: int = 0) -> None:
    if ctx.drafts is not None:
        try:
            await ctx.drafts(data["draft_id"], data["kind"], status, recipients)
        except Exception:  # noqa: BLE001 - a lost count must not stop the flow
            pass


def _patients(n: int) -> str:
    return f"{n} patient{'' if n == 1 else 's'}"


def _split_notice(text: str) -> tuple[str, str]:
    head, _, body = text.strip().partition("\n")
    return head.strip().removeprefix("Subject:").strip()[:200], body.strip()


class NotifyPatientsFlow(StagedFlow):
    id = "notify_patients"
    title = "Message a doctor's patients"
    description = "Draft a notice, edit it, then send it to everyone booked"
    icon = "message-square-text"
    roles = frozenset({"front_desk", "admin"})
    group = "Messages"
    keywords = (
        "notify",
        "notice",
        "tell her patients",
        "tell his patients",
        "message patients",
        "is away",
        "draft",
    )

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen in data.get("providers", {}):
            data["provider_id"] = chosen
            data["stage"] = "window"
            return None
        async with ctx.tool("list_providers", "Looking up doctors") as step:
            providers = await ctx.parta.providers(ctx.token, limit=200)
            step["summary"] = f"{len(providers)} doctors"
        data["providers"] = {p["id"]: f"{doctor(p)} · {p['specialty']}" for p in providers}
        # A doctor the user already named (handed over by the model): no need to ask.
        hint = (data.pop("provider_hint", "") or "").lower().removeprefix("dr ").strip()
        named = [
            pid
            for pid, label in data["providers"].items()
            if hint and hint in label.split(" · ")[0].lower()
        ]
        if len(named) == 1:
            data["provider_id"] = named[0]
            data["stage"] = "window"
            return None
        return AskOptions(
            "Whose patients should hear from us?",
            [
                Choice(pid, label.split(" · ")[0], label.split(" · ")[1])
                for pid, label in data["providers"].items()
            ],
            accepts_text=False,
        )

    async def on_window(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen not in {"1", "7", "14", "30"}:
            return AskOptions(
                "Patients booked over which period?",
                [
                    Choice("1", "Today"),
                    Choice("7", "Next 7 days"),
                    Choice("14", "Next 2 weeks"),
                    Choice("30", "Next month"),
                ],
                style="chips",
            )
        days = int(chosen)
        start = datetime.now(UTC)
        async with ctx.tool("appointments", "Finding who is booked") as step:
            items = await ctx.parta.appointments(
                ctx.token,
                provider_id=data["provider_id"],
                from_=start.isoformat(),
                to=(start + timedelta(days=days)).isoformat(),
            )
            live = [a for a in items if a["status"] in ("pending", "confirmed")]
            data["patients"] = sorted({a["patient_id"] for a in live if a.get("patient_id")})
            step["summary"] = f"{len(live)} appointments, {_patients(len(data['patients']))}"
        data["days"] = days
        if not data["patients"]:
            ctx.paragraph(
                f"Nobody is booked with {data['providers'][data['provider_id']].split(' · ')[0]} "
                "in that period, so there is no one to message."
            )
            data["stage"] = "done"
            return None
        data["stage"] = "brief"
        return None

    async def on_brief(self, ctx: Ctx, data: dict, answer: dict | None):
        # What the user already said they want patients told, if the model handed it over.
        text = typed(answer) or data.pop("brief_hint", None)
        if text is None:
            count = len(data["patients"])
            who = "this patient" if count == 1 else f"these {_patients(count)}"
            return AskText(
                f"What should {who} be told? "
                "For example: “Dr Osei is away next week; please choose a new time.”"
            )
        data["brief"] = text
        data["draft_id"] = data.get("draft_id") or uuid.uuid4().hex
        data["kind"] = "patient_notice"
        name = data["providers"][data["provider_id"]].split(" · ")[0]
        async with ctx.tool("draft", "Drafting the notice") as step:
            written = (
                await ctx.write(
                    NOTICE_INSTRUCTIONS,
                    (
                        f"Doctor: {name}\n"
                        f"Patients affected: booked in the next {data['days']} days\n"
                        f"What staff want to say: {text}"
                    ),
                )
                if ctx.write
                else None
            )
            step["summary"] = "written by the model" if written else "from the template"
        if written:
            data["subject"], data["body"] = _split_notice(written)
        if not written or not data["body"]:
            data["subject"] = f"About your appointment with {name}"
            data["body"] = (
                f"{text}\n\nPlease choose a new time in the patient portal or call "
                "the front desk. We're sorry for the inconvenience."
            )
        await _log(ctx, data, "generated")
        data["stage"] = "review"
        return None

    async def on_review(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen == "send":
            data["stage"] = "confirm"
            return None
        if chosen == "edit":
            data["stage"] = "edit"
            return None
        if chosen == "discard":
            await _log(ctx, data, "discarded")
            ctx.paragraph("Discarded. Nothing was sent.")
            data["stage"] = "done"
            return None
        ctx.paragraph("Here is the draft. Patients see it after “Hello <their name>,”:")
        ctx.card(
            "draft",
            data["subject"],
            subtitle="Draft notice",
            badge="Not sent",
            fields=[("Message", data["body"]), ("To", f"{_patients(len(data['patients']))}")],
        )
        return AskOptions(
            "What would you like to do?",
            [
                Choice("send", f"Send to {_patients(len(data['patients']))}"),
                Choice("edit", "Edit the message"),
                Choice("discard", "Discard"),
            ],
            style="chips",
        )

    async def on_edit(self, ctx: Ctx, data: dict, answer: dict | None):
        text = typed(answer)
        if text is None:
            return AskText(
                "Type the message as it should be sent (the greeting is added "
                "for you). The subject stays the same."
            )
        data["body"] = text
        data["stage"] = "review"
        return None

    async def on_confirm(self, ctx: Ctx, data: dict, answer: dict | None):
        count = len(data["patients"])
        if answer is None or "approved" not in answer:
            return AskConfirm(
                action="send_messages",
                title=f"Send this to {_patients(count)}?",
                details=[
                    ("Subject", data["subject"]),
                    ("Doctor", data["providers"][data["provider_id"]].split(" · ")[0]),
                ],
                confirm_label=f"Send to {count}",
                deny_label="Not yet",
            )
        if not answer["approved"]:
            data["stage"] = "review"
            return None
        data["stage"] = "done"
        async with ctx.tool("send", f"Sending to {_patients(count)}") as step:
            results = await asyncio.gather(
                *[
                    ctx.parta.send_message(
                        ctx.token,
                        patient_id=pid,
                        subject=data["subject"],
                        body=data["body"],
                        idempotency_key=f"{data['draft_id']}:{pid}",
                    )
                    for pid in data["patients"]
                ],
                return_exceptions=True,
            )
            outcome: dict[str, int] = {}
            for r in results:
                key = r["status"] if isinstance(r, dict) else "failed"
                outcome[key] = outcome.get(key, 0) + 1
            step["summary"] = ", ".join(f"{n} {k}" for k, n in outcome.items())
        await _log(ctx, data, "sent", outcome.get("queued", 0))
        queued = outcome.get("queued", 0)
        ctx.paragraph(
            "Sent. One message is on its way."
            if queued == 1
            else f"Sent. {queued} messages are on their way."
        )
        ctx.card(
            "sent",
            data["subject"],
            badge="Sent",
            fields=[
                ("Queued", str(outcome.get("queued", 0))),
                ("Already sent", str(outcome.get("duplicate", 0))),
                ("No email or phone", str(outcome.get("unreachable", 0))),
                ("Failed", str(outcome.get("failed", 0))),
            ],
        )
        return None


class PatientInstructionsFlow(StagedFlow):
    id = "patient_instructions"
    title = "Write patient instructions"
    description = "Turn your visit note into plain-language instructions you edit"
    icon = "notebook-pen"
    roles = frozenset({"provider"})
    group = "Patients"
    keywords = ("patient instructions", "patient summary", "write-up", "write up", "turn my notes")

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen in data.get("visits", {}):
            data["appointment_id"] = chosen
            data["stage"] = "draft"
            return None
        async with ctx.tool("my_schedule", "Finding your recent visits") as step:
            pid = await my_provider_id(ctx)
            now = datetime.now(UTC)
            items = await ctx.parta.appointments(
                ctx.token,
                provider_id=pid,
                from_=(now - timedelta(days=14)).isoformat(),
                to=(now + timedelta(hours=12)).isoformat(),
            )
            items = sorted(
                (
                    a
                    for a in items
                    if a["status"] in ("confirmed", "completed") and a.get("starts_at")
                ),
                key=lambda a: a["starts_at"],
                reverse=True,
            )[:8]
            step["summary"] = f"{len(items)} visits"
        if not items:
            raise FlowError("You have no visits in the last two weeks to write up.")
        names = await asyncio.gather(
            *[ctx.parta.patient(ctx.token, a["patient_id"]) for a in items], return_exceptions=True
        )
        data["visits"] = {
            a["id"]: (f"{p['first_name']} {p['last_name']}" if isinstance(p, dict) else "Patient")
            for a, p in zip(items, names, strict=True)
        }
        return AskOptions(
            "Which visit?",
            [
                Choice(
                    a["id"],
                    data["visits"][a["id"]],
                    ctx.when(a["starts_at"]),
                    "Completed" if a["status"] == "completed" else "",
                )
                for a in items
            ],
        )

    async def on_draft(self, ctx: Ctx, data: dict, answer: dict | None):
        try:
            async with ctx.tool("read_note", "Reading your note") as step:
                note = await ctx.parta.note(ctx.token, data["appointment_id"])
                step["summary"] = f"version {note['version']}, {note['status']}"
        except PartAError as exc:
            if exc.status == 404:
                raise FlowError(
                    "That visit has no note yet. Start it in the visit screen, then ask me again."
                ) from None
            raise
        if note["status"] != "draft":
            raise FlowError(
                "That note is signed. Amend it first (a new draft version), then "
                "ask me again — a signed note is never edited."
            )
        material = "\n".join(
            f"{label}: {note[key]}"
            for label, key in (
                ("Presenting complaint", "presenting_complaint"),
                ("Findings", "findings"),
                ("Clinician notes (do not quote)", "clinician_notes"),
            )
            if note.get(key)
        )
        material += "".join(f"\nDiagnosis: {d['description']}" for d in note.get("diagnoses", []))
        material += "".join(
            f"\nPrescribed: {p['medication']} {p['dose']}, {p['frequency']}"
            f"{', ' + p['duration'] if p.get('duration') else ''}"
            f"{'. ' + p['instructions'] if p.get('instructions') else ''}"
            for p in note.get("prescriptions", [])
        )
        if not material.strip():
            raise FlowError("The note is empty, so there is nothing to turn into instructions.")
        data["note"] = note
        data["draft_id"] = data.get("draft_id") or uuid.uuid4().hex
        data["kind"] = "patient_instructions"
        async with ctx.tool("draft", "Drafting the instructions") as step:
            written = await ctx.write(INSTRUCTIONS_INSTRUCTIONS, material) if ctx.write else None
            step["summary"] = "written by the model" if written else "from the note"
        data["summary"] = (
            written
            or "\n".join(
                [
                    *(f"Your diagnosis: {d['description']}." for d in note.get("diagnoses", [])),
                    *(
                        f"Take {p['medication']} {p['dose']}, {p['frequency']}"
                        f"{' for ' + p['duration'] if p.get('duration') else ''}. "
                        f"{p.get('instructions', '')}"
                        for p in note.get("prescriptions", [])
                    ),
                ]
            )
            or note.get("findings", "")
        )
        await _log(ctx, data, "generated")
        data["stage"] = "review"
        return None

    async def on_review(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen == "save":
            data["stage"] = "confirm"
            return None
        if chosen == "edit":
            data["stage"] = "edit"
            return None
        if chosen == "discard":
            await _log(ctx, data, "discarded")
            ctx.paragraph("Discarded. Your note is unchanged.")
            data["stage"] = "done"
            return None
        ctx.paragraph(
            f"Draft instructions for {data['visits'][data['appointment_id']]} — check "
            "every line against your note:"
        )
        ctx.card(
            "draft",
            "Patient instructions",
            subtitle="Draft — not saved",
            badge="Draft",
            fields=[("Instructions", data["summary"])],
        )
        return AskOptions(
            "What would you like to do?",
            [
                Choice("save", "Save into the note"),
                Choice("edit", "Edit"),
                Choice("discard", "Discard"),
            ],
            style="chips",
        )

    async def on_edit(self, ctx: Ctx, data: dict, answer: dict | None):
        text = typed(answer)
        if text is None:
            return AskText("Type the instructions as the patient should read them.")
        data["summary"] = text
        data["stage"] = "review"
        return None

    async def on_confirm(self, ctx: Ctx, data: dict, answer: dict | None):
        name = data["visits"][data["appointment_id"]]
        if answer is None or "approved" not in answer:
            return AskConfirm(
                action="save_patient_summary",
                title=f"Save these instructions into {name}'s note?",
                details=[
                    ("Note", f"version {data['note']['version']}, draft"),
                    ("Then", "you review and sign the note as usual"),
                ],
                confirm_label="Save into note",
                deny_label="Not yet",
            )
        if not answer["approved"]:
            data["stage"] = "review"
            return None
        data["stage"] = "done"
        note: dict[str, Any] = data["note"]
        body = {
            k: note.get(k)
            for k in (
                "presenting_complaint",
                "findings",
                "clinician_notes",
                "diagnoses",
                "prescriptions",
                "orders",
            )
        }
        body["clinician_notes"] = body["clinician_notes"] or ""
        body["patient_summary"] = data["summary"]
        async with ctx.tool("save_note", "Saving into the note") as step:
            saved = await ctx.parta.save_note(ctx.token, data["appointment_id"], body)
            step["summary"] = f"version {saved['version']}"
        await _log(ctx, data, "sent", 1)
        ctx.paragraph(
            f"Saved into {name}'s draft note. Review it and sign when you're ready — "
            "the patient sees it only after you sign."
        )
        ctx.card(
            "note",
            "Patient instructions saved",
            badge="Draft note",
            link=f"/bookings/{data['appointment_id']}",
        )
        return None
