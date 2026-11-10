"""Scheduling flows: book, reschedule, cancel, join the waitlist, see my appointments.

Every write here waits for an AskConfirm and runs only on the user's yes. A booking's
`Idempotency-Key` is minted when the confirmation is *asked* and kept in the flow's memory,
so a retried or replayed "yes" returns the same appointment (PRD D-11). Part A decides
everything that matters: who may book for whom, whether the slot is still free, whether
the patient is already booked then. A refusal is said to the user and the flow offers the
next sensible step.
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
    NotAnAnswer,
    doctor,
    option_id,
    typed,
)
from assistant_service.flows.staged import StagedFlow
from assistant_service.parta import PartAError

SEARCH_DAYS = 14
LIVE = ("pending", "confirmed")


def _window(days: int = SEARCH_DAYS) -> tuple[str, str]:
    now = datetime.now(UTC)
    return now.isoformat(), (now + timedelta(days=days)).isoformat()


def _match(text: str, candidates: list[str]) -> str | None:
    lowered = text.lower()
    for candidate in candidates:
        if candidate.lower() == lowered:
            return candidate
    hits = [c for c in candidates if lowered in c.lower() or c.lower() in lowered]
    return hits[0] if len(hits) == 1 else None


async def wait_for_outcome(
    ctx: Ctx, appointment_id: str, *, attempts: int = 20, delay: float = 0.5
) -> dict:
    """A booking is accepted (202) and confirmed by its workflow a moment later."""
    appointment: dict = {}
    for _ in range(attempts):
        appointment = await ctx.parta.appointment(ctx.token, appointment_id)
        if appointment["status"] != "pending":
            return appointment
        await asyncio.sleep(delay)
    return appointment


class _PatientStages:
    """Who the appointment is for: the caller, or (staff) a patient they find."""

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        if ctx.role == "patient":
            async with ctx.tool("my_record", "Checking your patient record") as step:
                me = await ctx.parta.me_patient(ctx.token)
                step["summary"] = f"{me['first_name']} {me['last_name']}"
            data["patient_id"] = me["id"]
            data["patient_name"] = f"{me['first_name']} {me['last_name']}"
            data["stage"] = self.after_patient
            return None
        if data.get("patient_id"):
            data["stage"] = self.after_patient
            return None
        data["stage"] = "find_patient"
        return AskText("Which patient is this for? Type their MRN or name.")

    async def on_find_patient(self, ctx: Ctx, data: dict, answer: dict | None):
        text = typed(answer)
        if text is None:
            return AskText("Which patient is this for? Type their MRN or name.")
        async with ctx.tool("search_patients", f"Searching patients for “{text}”") as step:
            found = await ctx.parta.patients(ctx.token, text)
            step["summary"] = f"{len(found)} found"
        if not found:
            ctx.paragraph(f"I couldn't find a patient matching “{text}”.")
            return AskText("Try another MRN or name.")
        if len(found) == 1:
            self._take_patient(data, found[0])
            data["stage"] = self.after_patient
            return None
        data["stage"] = "pick_patient"
        data["patients"] = {p["id"]: f"{p['first_name']} {p['last_name']}" for p in found[:8]}
        return AskOptions(
            "Which of these patients?",
            [
                Choice(
                    p["id"],
                    f"{p['first_name']} {p['last_name']}",
                    f"MRN {p['mrn']}"
                    + (f" · born {p['date_of_birth']}" if p.get("date_of_birth") else ""),
                )
                for p in found[:8]
            ],
            accepts_text=True,
        )

    async def on_pick_patient(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen in data.get("patients", {}):
            data["patient_id"] = chosen
            data["patient_name"] = data["patients"][chosen]
            data["stage"] = self.after_patient
            return None
        if typed(answer):
            data["stage"] = "find_patient"
            return await self.on_find_patient(ctx, data, answer)
        return AskOptions(
            "Which of these patients?",
            [Choice(k, v) for k, v in data.get("patients", {}).items()],
            accepts_text=True,
        )

    @staticmethod
    def _take_patient(data: dict, patient: dict) -> None:
        data["patient_id"] = patient["id"]
        data["patient_name"] = f"{patient['first_name']} {patient['last_name']}"


class _ProviderStages:
    """Specialty, then a doctor (or "first available"), each with its next free time."""

    async def on_specialty(self, ctx: Ctx, data: dict, answer: dict | None):
        async with ctx.tool("list_providers", "Looking up specialties") as step:
            providers = await ctx.parta.providers(ctx.token)
            specialties = sorted({p["specialty"] for p in providers})
            step["summary"] = f"{len(specialties)} specialties"
        if data.get("specialty"):
            chosen = _match(data["specialty"], specialties)
            if chosen:
                data["specialty"] = chosen
                data["stage"] = "provider"
                return None
            ctx.paragraph(f"We don't have a “{data.pop('specialty')}” specialty.")
        chosen = option_id(answer) or (typed(answer) and _match(typed(answer), specialties))
        if chosen in specialties:
            data["specialty"] = chosen
            data["stage"] = "provider"
            return None
        if typed(answer):
            raise NotAnAnswer(typed(answer))
        return AskOptions(
            "Which kind of doctor do you need?",
            [Choice(s, s) for s in specialties],
            style="chips",
            accepts_text=True,
        )

    async def on_provider(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen is not None and chosen in data.get("providers", {}) | {"any": ""}:
            data["provider_id"] = chosen
            data["stage"] = "slot"
            return None
        start, end = _window()
        async with ctx.tool(
            "free_slots", f"Finding free {data['specialty'].lower()} times"
        ) as step:
            providers = (await ctx.parta.providers(ctx.token, specialty=data["specialty"]))[:6]
            nexts = await asyncio.gather(
                *[
                    ctx.parta.slots(ctx.token, p["id"], start=start, end=end, limit=1)
                    for p in providers
                ]
            )
            bookable = [(p, s[0]) for p, s in zip(providers, nexts, strict=True) if s]
            step["summary"] = f"{len(bookable)} of {len(providers)} doctors have free times"
        if not bookable:
            ctx.paragraph(
                f"No {data['specialty']} doctor has a free time in the next {SEARCH_DAYS} days."
            )
            data["stage"] = "no_slots"
            return None
        bookable.sort(key=lambda pair: pair[1]["starts_at"])
        data["providers"] = {p["id"]: doctor(p) for p, _ in bookable}
        if data.get("provider_hint"):
            chosen = _match(data.pop("provider_hint"), list(data["providers"].values()))
            for pid, name in data["providers"].items():
                if name == chosen:
                    data["provider_id"] = pid
                    data["stage"] = "slot"
                    return None
        options = [
            Choice(
                "any",
                "First available",
                f"Earliest: {ctx.when(bookable[0][1]['starts_at'])} with {doctor(bookable[0][0])}",
                "Fastest",
            )
        ]
        options += [
            Choice(p["id"], doctor(p), p["specialty"], f"Next: {ctx.when(s['starts_at'])}")
            for p, s in bookable
        ]
        return AskOptions("Who would you like to see?", options)

    async def on_no_slots(self, ctx: Ctx, data: dict, answer: dict | None):
        ctx.suggest("Join a waitlist", "Book an appointment")
        data["stage"] = "done"
        return None


class BookFlow(_PatientStages, _ProviderStages, StagedFlow):
    id = "book"
    title = "Book an appointment"
    description = "Pick a specialty, a doctor and a time"
    icon = "calendar-plus"
    roles = frozenset({"patient", "front_desk", "admin"})
    group = "Appointments"
    keywords = ("book", "appointment with", "see a doctor", "schedule", "walk-in", "walk in")
    after_patient = "specialty"

    async def on_slot(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen == "later":
            data["slot_offset"] = data.get("slot_offset", 0) + 6
        elif chosen in data.get("slots", {}):
            data["slot"] = data["slots"][chosen]
            data["stage"] = "confirm"
            return None
        start, end = _window()
        ids = list(data["providers"]) if data["provider_id"] == "any" else [data["provider_id"]]
        label = (
            "Finding the earliest times"
            if data["provider_id"] == "any"
            else f"Finding {data['providers'][data['provider_id']]}'s free times"
        )
        async with ctx.tool("free_slots", label) as step:
            offset = data.get("slot_offset", 0)
            pages = await asyncio.gather(
                *[
                    ctx.parta.slots(ctx.token, pid, start=start, end=end, limit=offset + 6)
                    for pid in ids
                ]
            )
            slots = sorted((s for page in pages for s in page), key=lambda s: s["starts_at"])
            window = slots[offset : offset + 6]
            step["summary"] = f"{len(window)} times shown"
        if not window:
            ctx.paragraph("There are no more free times in the next two weeks.")
            data["stage"] = "no_slots"
            return None
        data["slots"] = {
            s["id"]: {"id": s["id"], "starts_at": s["starts_at"], "provider_id": s["provider_id"]}
            for s in window
        }
        options = [
            Choice(
                s["id"],
                ctx.when(s["starts_at"]),
                data["providers"].get(s["provider_id"], ""),
                "Earliest" if i == 0 and offset == 0 else "",
            )
            for i, s in enumerate(window)
        ]
        options.append(Choice("later", "Show later times"))
        return AskOptions("Which time suits you?", options)

    async def on_confirm(self, ctx: Ctx, data: dict, answer: dict | None):
        if answer is None or "approved" not in answer:
            data.setdefault("idempotency", uuid.uuid4().hex)
            slot = data["slot"]
            return AskConfirm(
                action="book_appointment",
                title="Book this appointment?",
                details=[
                    ("Patient", data["patient_name"]),
                    ("Doctor", data["providers"][slot["provider_id"]]),
                    ("Specialty", data["specialty"]),
                    ("When", ctx.when(slot["starts_at"])),
                ],
                confirm_label="Book it",
                deny_label="Not now",
            )
        if not answer["approved"]:
            ctx.paragraph("Okay, I haven't booked anything.")
            ctx.suggest("Book an appointment", "Join a waitlist")
            data["stage"] = "done"
            return None
        slot = data["slot"]
        try:
            async with ctx.tool("book", "Booking the appointment") as step:
                accepted = await ctx.parta.book(
                    ctx.token,
                    patient_id=data["patient_id"],
                    slot_id=slot["id"],
                    reason="Booked with the assistant",
                    idempotency_key=f"assistant-{data['idempotency']}",
                    conversation_ref=ctx.conversation_id,
                )
                outcome = await wait_for_outcome(ctx, accepted["appointment_id"])
                step["summary"] = outcome["status"]
        except PartAError as exc:
            ctx.paragraph(f"The booking system said no: {exc.detail}")
            data.pop("idempotency", None)
            data["stage"] = "slot"
            data["slot_offset"] = 0
            return None
        data["stage"] = "done"
        if outcome["status"] == "confirmed":
            ctx.paragraph(
                f"Done — {data['patient_name']} is booked with "
                f"{data['providers'][slot['provider_id']]}."
            )
            ctx.card(
                "appointment",
                "Appointment confirmed",
                badge="Confirmed",
                subtitle=data["specialty"],
                fields=[
                    ("Doctor", data["providers"][slot["provider_id"]]),
                    ("When", ctx.when(slot["starts_at"])),
                    ("Patient", data["patient_name"]),
                ],
                link=f"/bookings/{outcome['id']}",
            )
            ctx.suggest("How do I prepare?", "Show my appointments", "Cancel this appointment")
        elif outcome["status"] == "pending":
            ctx.paragraph(
                "The booking is still being confirmed. It will show in your "
                "appointments in a moment."
            )
            ctx.card(
                "appointment",
                "Booking in progress",
                badge="Pending",
                link=f"/bookings/{outcome['id']}",
            )
        else:
            ctx.paragraph(
                "That booking didn't go through — the time may have just been "
                "taken, or a payment check failed. Nothing was charged."
            )
            data.pop("idempotency", None)
            ctx.suggest("Book an appointment", "Join a waitlist")
        return None


async def upcoming(ctx: Ctx, *, patient_id: str | None = None) -> list[dict]:
    items = await ctx.parta.appointments(
        ctx.token, patient_id=patient_id, from_=datetime.now(UTC).isoformat()
    )
    return sorted(
        (a for a in items if a["status"] in LIVE and a.get("starts_at")),
        key=lambda a: a["starts_at"],
    )


async def provider_names(ctx: Ctx, appointments: list[dict]) -> dict[str, str]:
    ids = {a["provider_id"] for a in appointments if a.get("provider_id")}
    found = await asyncio.gather(
        *[ctx.parta.provider(ctx.token, pid) for pid in ids], return_exceptions=True
    )
    return {p["id"]: doctor(p) for p in found if isinstance(p, dict)}


class _PickAppointment:
    verb = "change"

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        async with ctx.tool("my_appointments", "Looking up your upcoming appointments") as step:
            items = await upcoming(ctx, patient_id=data.get("patient_id"))
            names = await provider_names(ctx, items)
            step["summary"] = f"{len(items)} upcoming"
        if not items:
            ctx.paragraph("You have no upcoming appointments.")
            ctx.suggest("Book an appointment")
            data["stage"] = "done"
            return None
        data["appointments"] = {
            a["id"]: {
                "id": a["id"],
                "starts_at": a["starts_at"],
                "provider_id": a["provider_id"],
                "doctor": names.get(a["provider_id"], "your doctor"),
                "status": a["status"],
            }
            for a in items[:8]
        }
        if len(items) == 1:
            data["appointment"] = next(iter(data["appointments"].values()))
            data["stage"] = "chosen"
            return None
        data["stage"] = "pick"
        return self._ask(ctx, data)

    def _ask(self, ctx: Ctx, data: dict) -> AskOptions:
        return AskOptions(
            f"Which appointment do you want to {self.verb}?",
            [
                Choice(
                    a["id"],
                    ctx.when(a["starts_at"]),
                    a["doctor"],
                    "Pending" if a["status"] == "pending" else "",
                )
                for a in data["appointments"].values()
            ],
        )

    async def on_pick(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen in data["appointments"]:
            data["appointment"] = data["appointments"][chosen]
            data["stage"] = "chosen"
            return None
        return self._ask(ctx, data)


class CancelFlow(_PickAppointment, StagedFlow):
    id = "cancel"
    title = "Cancel an appointment"
    description = "Free the time for someone else"
    icon = "calendar-x"
    roles = frozenset({"patient"})
    group = "Appointments"
    keywords = ("cancel",)
    verb = "cancel"

    async def on_chosen(self, ctx: Ctx, data: dict, answer: dict | None):
        if answer is None or "approved" not in answer:
            a = data["appointment"]
            return AskConfirm(
                action="cancel_appointment",
                title="Cancel this appointment?",
                details=[("Doctor", a["doctor"]), ("When", ctx.when(a["starts_at"]))],
                confirm_label="Cancel appointment",
                deny_label="Keep it",
                tone="destructive",
            )
        data["stage"] = "done"
        if not answer["approved"]:
            ctx.paragraph("Okay, your appointment stays as it is.")
            return None
        a = data["appointment"]
        try:
            async with ctx.tool("cancel", "Cancelling the appointment") as step:
                result = await ctx.parta.cancel(ctx.token, a["id"], "Cancelled with the assistant")
                step["summary"] = result["status"]
        except PartAError as exc:
            ctx.paragraph(f"I couldn't cancel it: {exc.detail}")
            return None
        ctx.paragraph(
            "Cancelled. Any refund due is processed automatically, and the time is "
            "offered to patients on the waitlist."
        )
        ctx.card(
            "appointment",
            "Appointment cancelled",
            badge="Cancelled",
            fields=[("Doctor", a["doctor"]), ("Was", ctx.when(a["starts_at"]))],
            link=f"/bookings/{a['id']}",
        )
        ctx.suggest("Book another time", "Show my appointments")
        return None


class RescheduleFlow(_PickAppointment, StagedFlow):
    id = "reschedule"
    title = "Move an appointment"
    description = "Same doctor, a different time"
    icon = "calendar-clock"
    roles = frozenset({"patient"})
    group = "Appointments"
    keywords = ("reschedule", "move my", "change my appointment", "different time")
    verb = "move"

    async def on_chosen(self, ctx: Ctx, data: dict, answer: dict | None):
        a = data["appointment"]
        if a["status"] != "confirmed":
            ctx.paragraph("That booking is still being confirmed; try again in a moment.")
            data["stage"] = "done"
            return None
        chosen = option_id(answer)
        if chosen in data.get("slots", {}):
            data["slot"] = data["slots"][chosen]
            data["stage"] = "confirm"
            return None
        start, end = _window(21)
        async with ctx.tool("free_slots", f"Finding {a['doctor']}'s free times") as step:
            slots = await ctx.parta.slots(
                ctx.token, a["provider_id"], start=start, end=end, limit=6
            )
            step["summary"] = f"{len(slots)} free"
        if not slots:
            ctx.paragraph(f"{a['doctor']} has no free times in the next three weeks.")
            ctx.suggest("Join a waitlist", "Book an appointment")
            data["stage"] = "done"
            return None
        data["slots"] = {s["id"]: {"id": s["id"], "starts_at": s["starts_at"]} for s in slots}
        return AskOptions(
            f"Move it to when? (now {ctx.when(a['starts_at'])})",
            [Choice(s["id"], ctx.when(s["starts_at"]), a["doctor"]) for s in slots],
        )

    async def on_confirm(self, ctx: Ctx, data: dict, answer: dict | None):
        a = data["appointment"]
        if answer is None or "approved" not in answer:
            data.setdefault("idempotency", uuid.uuid4().hex)
            return AskConfirm(
                action="reschedule_appointment",
                title="Move this appointment?",
                details=[
                    ("Doctor", a["doctor"]),
                    ("From", ctx.when(a["starts_at"])),
                    ("To", ctx.when(data["slot"]["starts_at"])),
                ],
                confirm_label="Move it",
                deny_label="Keep the old time",
            )
        data["stage"] = "done"
        if not answer["approved"]:
            ctx.paragraph("Okay, the appointment keeps its time.")
            return None
        try:
            async with ctx.tool("reschedule", "Moving the appointment") as step:
                accepted = await ctx.parta.reschedule(
                    ctx.token,
                    a["id"],
                    new_slot_id=data["slot"]["id"],
                    idempotency_key=f"assistant-{data['idempotency']}",
                    conversation_ref=ctx.conversation_id,
                )
                outcome = await wait_for_outcome(ctx, accepted["appointment_id"])
                step["summary"] = outcome["status"]
        except PartAError as exc:
            ctx.paragraph(f"I couldn't move it: {exc.detail}")
            return None
        if outcome["status"] == "confirmed":
            ctx.paragraph("Moved. Your old time has been released.")
            ctx.card(
                "appointment",
                "Appointment moved",
                badge="Confirmed",
                fields=[
                    ("Doctor", a["doctor"]),
                    ("When", ctx.when(data["slot"]["starts_at"])),
                    ("Was", ctx.when(a["starts_at"])),
                ],
                link=f"/bookings/{outcome['id']}",
            )
        else:
            ctx.paragraph(
                "The new time didn't go through, so your original appointment stands unchanged."
            )
        return None


class WaitlistFlow(_PatientStages, _ProviderStages, StagedFlow):
    id = "waitlist"
    title = "Join a waitlist"
    description = "Be offered a time if one frees up"
    icon = "list-checks"
    roles = frozenset({"patient", "front_desk"})
    group = "Appointments"
    keywords = ("waitlist", "waiting list")
    after_patient = "specialty"

    async def on_provider(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen in data.get("providers", {}):
            data["provider_id"] = chosen
            data["stage"] = "window"
            return None
        async with ctx.tool(
            "list_providers", f"Looking up {data['specialty'].lower()} doctors"
        ) as step:
            providers = await ctx.parta.providers(ctx.token, specialty=data["specialty"])
            step["summary"] = f"{len(providers)} doctors"
        if not providers:
            raise FlowError(f"There are no {data['specialty']} doctors to wait for.")
        data["providers"] = {p["id"]: doctor(p) for p in providers[:8]}
        return AskOptions(
            "Whose waitlist?", [Choice(pid, name) for pid, name in data["providers"].items()]
        )

    async def on_window(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen in {"7", "14", "30"}:
            data["days"] = int(chosen)
            data["stage"] = "confirm"
            return None
        return AskOptions(
            "For how far ahead?",
            [Choice("7", "Next 7 days"), Choice("14", "Next 2 weeks"), Choice("30", "Next month")],
            style="chips",
        )

    async def on_confirm(self, ctx: Ctx, data: dict, answer: dict | None):
        name = data["providers"][data["provider_id"]]
        if answer is None or "approved" not in answer:
            return AskConfirm(
                action="join_waitlist",
                title="Join this waitlist?",
                details=[
                    ("Patient", data["patient_name"]),
                    ("Doctor", name),
                    ("For", f"the next {data['days']} days"),
                ],
                confirm_label="Join waitlist",
                deny_label="Not now",
            )
        data["stage"] = "done"
        if not answer["approved"]:
            ctx.paragraph("Okay, not added.")
            return None
        start, end = _window(data["days"])
        async with ctx.tool("join_waitlist", "Adding you to the waitlist") as step:
            entry = await ctx.parta.join_waitlist(
                ctx.token,
                patient_id=data["patient_id"],
                provider_id=data["provider_id"],
                window_start=start,
                window_end=end,
            )
            step["summary"] = entry["status"]
        ctx.paragraph(
            f"Added. If a time with {name} frees up, it is offered "
            "automatically and you'll get a message."
        )
        ctx.card(
            "waitlist",
            "On the waitlist",
            badge="Waiting",
            fields=[("Doctor", name), ("Until", ctx.day(end))],
        )
        return None


class MyAppointmentsFlow(StagedFlow):
    id = "my_appointments"
    title = "My appointments"
    description = "What's coming up"
    icon = "clipboard-list"
    roles = frozenset({"patient"})
    group = "Appointments"
    keywords = ("my appointments", "upcoming", "when is my", "next appointment")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        async with ctx.tool("my_appointments", "Looking up your appointments") as step:
            items = await upcoming(ctx)
            names = await provider_names(ctx, items)
            step["summary"] = f"{len(items)} upcoming"
        if not items:
            ctx.paragraph("You have no upcoming appointments.")
            ctx.suggest("Book an appointment")
            return None
        ctx.paragraph(f"You have {len(items)} upcoming appointment{'s' if len(items) > 1 else ''}:")
        for a in items[:6]:
            ctx.card(
                "appointment",
                names.get(a["provider_id"], "Appointment"),
                subtitle=ctx.when(a["starts_at"]),
                badge=a["status"].capitalize(),
                link=f"/bookings/{a['id']}",
            )
        ctx.suggest("Move an appointment", "Cancel an appointment", "How do I prepare?")
        return None
