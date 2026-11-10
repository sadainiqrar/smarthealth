"""Operational flows: the day's schedule, free capacity, no-shows, the weekly numbers.

**Every number comes from a tool, never from the model** (spec 9.1): these flows fetch
counts from Part A and print them; with a model present, it may write the narrative around
them but is given the same numbers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

from assistant_service.flows.base import (
    NotAnAnswer,
    AskOptions,
    Choice,
    Ctx,
    doctor,
    option_id,
    typed,
)
from assistant_service.flows.record import my_provider_id
from assistant_service.flows.scheduling import _match
from assistant_service.flows.staged import StagedFlow


def _today(ctx: Ctx) -> tuple[str, str]:
    local = datetime.now(ctx.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.isoformat(), (local + timedelta(days=1)).isoformat()


class MyDayFlow(StagedFlow):
    id = "my_day"
    title = "My day"
    description = "Today's patients: checked in, waiting, seen"
    icon = "clipboard-list"
    roles = frozenset({"provider"})
    group = "Today"
    keywords = ("my day", "today", "checked in", "waited longest", "schedule")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        start, end = _today(ctx)
        async with ctx.tool("my_schedule", "Looking up today's appointments") as step:
            pid = await my_provider_id(ctx)
            items = await ctx.parta.appointments(ctx.token, provider_id=pid, from_=start, to=end)
            items = sorted((a for a in items if a.get("starts_at")), key=lambda a: a["starts_at"])
            step["summary"] = f"{len(items)} today"
        if not items:
            ctx.paragraph("You have no appointments today.")
            return None
        patients = await asyncio.gather(
            *[ctx.parta.patient(ctx.token, a["patient_id"]) for a in items], return_exceptions=True
        )
        counts: dict[str, int] = {}
        for a in items:
            label = "in progress" if a.get("in_progress") else a["status"]
            counts[label] = counts.get(label, 0) + 1
        ctx.paragraph(
            "Today: " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in counts.items()) + "."
        )
        for a, p in zip(items, patients, strict=True):
            name = f"{p['first_name']} {p['last_name']}" if isinstance(p, dict) else "Patient"
            ctx.card(
                "appointment",
                name,
                subtitle=ctx.when(a["starts_at"]),
                badge="In progress"
                if a.get("in_progress")
                else a["status"].replace("_", " ").capitalize(),
                fields=[("Reason", a.get("reason") or "")],
                link=f"/bookings/{a['id']}",
            )
        ctx.suggest("Brief me on my next patient", "Results to review")
        return None


class CapacityFlow(StagedFlow):
    id = "capacity"
    title = "Who's free?"
    description = "Free times by specialty, today and tomorrow"
    icon = "users"
    roles = frozenset({"front_desk", "admin"})
    group = "Today"
    keywords = ("who's free", "who is free", "capacity", "free in", "availability")

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        async with ctx.tool("list_providers", "Looking up specialties") as step:
            providers = await ctx.parta.providers(ctx.token, limit=200)
            specialties = sorted({p["specialty"] for p in providers})
            step["summary"] = f"{len(specialties)} specialties"
        chosen = option_id(answer) or (typed(answer) and _match(typed(answer), specialties))
        if chosen not in specialties and typed(answer):
            raise NotAnAnswer(typed(answer))
        if chosen not in specialties:
            return AskOptions(
                "Which specialty?",
                [Choice(s, s) for s in specialties],
                style="chips",
                accepts_text=True,
            )
        data["stage"] = "done"
        start = datetime.now(UTC)
        end = datetime.now(ctx.timezone).replace(hour=0, minute=0) + timedelta(days=2)
        doctors = [p for p in providers if p["specialty"] == chosen]
        async with ctx.tool("free_slots", f"Finding free {chosen.lower()} times") as step:
            pages = await asyncio.gather(
                *[
                    ctx.parta.slots(
                        ctx.token, p["id"], start=start.isoformat(), end=end.isoformat(), limit=50
                    )
                    for p in doctors
                ]
            )
            step["summary"] = f"{sum(len(p) for p in pages)} free times"
        free = [(p, s) for p, s in zip(doctors, pages, strict=True) if s]
        if not free:
            ctx.paragraph(f"No {chosen} doctor has a free time today or tomorrow.")
            ctx.suggest("Join a waitlist")
            return None
        ctx.paragraph(f"{chosen}, today and tomorrow:")
        for p, slots in sorted(free, key=lambda pair: pair[1][0]["starts_at"]):
            ctx.card(
                "capacity",
                doctor(p),
                subtitle=f"{len(slots)} free",
                badge=f"Next {ctx.when(slots[0]['starts_at'])}",
                fields=[
                    ("Times", ", ".join(ctx.when(s["starts_at"]).split(", ")[1] for s in slots[:8]))
                ],
            )
        ctx.suggest("Book a walk-in")
        return None


class NoShowsFlow(StagedFlow):
    id = "no_shows"
    title = "Today's no-shows"
    description = "Who didn't come, and who is waiting for those doctors"
    icon = "calendar-x"
    roles = frozenset({"front_desk", "admin"})
    group = "Today"
    keywords = ("no-show", "no show", "didn't come", "missed")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        start, end = _today(ctx)
        async with ctx.tool("appointments", "Looking up today's no-shows") as step:
            missed = await ctx.parta.appointments(ctx.token, status="no_show", from_=start, to=end)
            step["summary"] = f"{len(missed)} no-shows"
        if not missed:
            ctx.paragraph("No no-shows today so far.")
            return None
        async with ctx.tool("waitlist", "Checking the waitlist") as step:
            waiting = [w for w in await ctx.parta.waitlist(ctx.token) if w["status"] == "active"]
            step["summary"] = f"{len(waiting)} waiting"
        names = {}
        for pid in {a["provider_id"] for a in missed}:
            try:
                names[pid] = doctor(await ctx.parta.provider(ctx.token, pid))
            except Exception:  # noqa: BLE001 - a name is cosmetic
                names[pid] = "Doctor"
        for a in missed:
            queue = [w for w in waiting if w.get("provider_id") == a["provider_id"]]
            ctx.card(
                "appointment",
                names[a["provider_id"]],
                subtitle=ctx.when(a["starts_at"]),
                badge="No-show",
                fields=[("Waitlisted for this doctor", str(len(queue)))],
                link=f"/bookings/{a['id']}",
            )
        return None


class SnapshotFlow(StagedFlow):
    id = "snapshot"
    title = "Operations snapshot"
    description = "This week against last week"
    icon = "chart-column"
    roles = frozenset({"admin"})
    group = "Operations"
    keywords = ("snapshot", "report", "this week", "operations", "kpi", "numbers")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        today = date.today()
        this_start, last_start = today - timedelta(days=6), today - timedelta(days=13)
        async with ctx.tool("analytics", "Fetching the numbers") as step:
            this, last = await asyncio.gather(
                ctx.parta.analytics_summary(
                    ctx.token, start=this_start.isoformat(), end=today.isoformat()
                ),
                ctx.parta.analytics_summary(
                    ctx.token,
                    start=last_start.isoformat(),
                    end=(this_start - timedelta(days=1)).isoformat(),
                ),
            )
            step["summary"] = f"{this['appointments_booked']} booked this week"

        def delta(key: str) -> str:
            a, b = this[key], last[key]
            if a is None:
                return "—"
            if b in (None, 0):
                return f"{a}"
            change = (a - b) / b * 100
            return f"{a} ({'+' if change >= 0 else ''}{change:.0f}% vs last week)"

        def rate(value: float | None) -> str:
            return "—" if value is None else f"{value * 100:.1f}%"

        def wait(value: float | None) -> str:
            return "—" if value is None else f"{value / 60:.0f} min"

        ctx.paragraph(f"The last seven days ({this_start:%d %b} – {today:%d %b}):")
        ctx.card(
            "metrics",
            "This week",
            subtitle="All clinics",
            fields=[
                ("Booked", delta("appointments_booked")),
                ("Completed visits", delta("completed_visits")),
                ("Cancellations", delta("cancellations")),
                ("No-shows", delta("no_shows")),
                (
                    "Cancellation rate",
                    f"{rate(this['cancellation_rate'])} (was {rate(last['cancellation_rate'])})",
                ),
                (
                    "Average wait",
                    f"{wait(this['average_wait_seconds'])} "
                    f"(was {wait(last['average_wait_seconds'])})",
                ),
                ("Patients", str(this["total_patients"])),
            ],
        )
        ctx.paragraph(
            "Every number here is read from the analytics service; Grafana's "
            "*Platform analytics* dashboard has the daily detail."
        )
        return None
