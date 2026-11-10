"""Clinical-record flows (spec 9.1, 9.6): what the record says, restated -- never advice.

The rules that make these safe are in the wording as much as the code:

- a prescription is restated **word for word** as the doctor wrote it; anything beyond it is
  "ask your doctor or pharmacist" -- the assistant never adjusts a dose;
- a report is shown as the values printed on it, marked not yet reviewed until the
  ordering doctor has, and always ends with "your doctor will review it" (O-7) -- never a
  diagnosis;
- preparation comes from the clinic's own guide (knowledge), cited, tied to the actual
  order the doctor placed;
- a provider reads a record only through Part A's care-relationship check (R-14): this
  code asks, Part A answers 404 if they have no relationship, and the flow says so.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from assistant_service.flows.base import AskOptions, Choice, Ctx, FlowError, option_id
from assistant_service.flows.scheduling import provider_names
from assistant_service.flows.staged import StagedFlow
from assistant_service.parta import PartAError

ASK_YOUR_DOCTOR = (
    "If anything is unclear, or you have side effects, ask your doctor or "
    "pharmacist — I can't change or advise on doses."
)


def _signed(record: dict) -> list[dict]:
    return sorted(
        (n for n in record["notes"] if n["status"] == "signed"),
        key=lambda n: n["signed_at"] or "",
        reverse=True,
    )


async def guide_for(ctx: Ctx, order: dict) -> dict | None:
    """The section of the clinic's guide the catalogue names for this test."""
    hits = await ctx.search(f"how to prepare for {order['test_name']}", 5)
    wanted = order.get("prep_source_id") or ""
    for hit in hits:
        if wanted and hit["source_id"] == wanted:
            return hit
    for hit in hits:
        if wanted and hit["source_id"].split("#")[0] == wanted.split("#")[0]:
            return hit
    return hits[0] if hits else None


class PrepareFlow(StagedFlow):
    id = "prepare"
    title = "Prepare for a test"
    description = "What to do before the tests your doctor ordered"
    icon = "flask-conical"
    roles = frozenset({"patient"})
    group = "My health"
    keywords = (
        "prepare",
        "preparation",
        "fast before",
        "fasting",
        "before my test",
        "tests did my doctor",
    )

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        async with ctx.tool("my_record", "Looking up the tests your doctor ordered") as step:
            record = await ctx.parta.my_record(ctx.token)
            orders = [o for o in record["orders"] if o["status"] == "ordered"]
            step["summary"] = f"{len(orders)} waiting for a result"
        if not orders:
            ctx.paragraph(
                "You have no tests waiting to be done. If you expected one, your "
                "doctor may not have signed the visit note yet."
            )
            ctx.suggest("Show my appointments", "What did the doctor say last time?")
            data["stage"] = "done"
            return None
        data["orders"] = {
            o["id"]: {
                k: o[k] for k in ("id", "test_name", "prep_source_id", "urgency", "created_at")
            }
            for o in orders
        }
        if len(orders) == 1:
            data["order"] = orders[0]["id"]
            data["stage"] = "explain"
            return None
        data["stage"] = "pick"
        return self._ask(ctx, data)

    def _ask(self, ctx: Ctx, data: dict) -> AskOptions:
        return AskOptions(
            "Which test?",
            [
                Choice(
                    o["id"],
                    o["test_name"],
                    f"Ordered {ctx.day(o['created_at'])}",
                    "Urgent" if o["urgency"] == "urgent" else "",
                )
                for o in data["orders"].values()
            ]
            + [Choice("all", "All of them")],
        )

    async def on_pick(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen == "all" or chosen in data["orders"]:
            data["order"] = chosen
            data["stage"] = "explain"
            return None
        return self._ask(ctx, data)

    async def on_explain(self, ctx: Ctx, data: dict, answer: dict | None):
        data["stage"] = "done"
        orders = (
            list(data["orders"].values())
            if data["order"] == "all"
            else [data["orders"][data["order"]]]
        )
        async with ctx.tool("search_knowledge", "Reading the clinic's preparation guides") as step:
            guides = await asyncio.gather(*[guide_for(ctx, o) for o in orders])
            step["summary"] = f"{sum(1 for g in guides if g)} guides found"
        for order, guide in zip(orders, guides, strict=True):
            ctx.say(
                f"**{order['test_name']}**"
                + (" (urgent)" if order["urgency"] == "urgent" else "")
                + "\n\n"
            )
            if guide is None:
                ctx.paragraph(
                    "I couldn't find a preparation guide for this test — please ask the clinic."
                )
                continue
            ctx.paragraph(guide["snippet"])
            ctx.cite(guide["source_id"], guide["title"], guide["snippet"][:240])
        ctx.paragraph(
            "This comes from the clinic's own guides. If your doctor gave you "
            "different instructions, follow theirs."
        )
        ctx.suggest("Book my test", "Upload a result", "Show my appointments")
        return None


class PrescriptionsFlow(StagedFlow):
    id = "prescriptions"
    title = "My prescriptions"
    description = "How to take what you were prescribed"
    icon = "pill"
    roles = frozenset({"patient"})
    group = "My health"
    keywords = ("prescription", "medication", "medicine", "how should i take", "dose", "pill")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        async with ctx.tool("my_record", "Looking up your prescriptions") as step:
            record = await ctx.parta.my_record(ctx.token)
            active = [p for p in record["prescriptions"] if p["status"] == "active"]
            step["summary"] = f"{len(active)} active"
        if not active:
            ctx.paragraph("You have no active prescriptions on record.")
            return None
        ctx.paragraph("Here is exactly what your doctor prescribed:")
        for p in active:
            ctx.card(
                "prescription",
                p["medication"],
                subtitle=p["dose"],
                badge="Active",
                fields=[
                    ("How often", p["frequency"]),
                    ("For how long", p["duration"]),
                    ("Instructions", p["instructions"]),
                ],
            )
        ctx.paragraph(ASK_YOUR_DOCTOR)
        return None


class LastVisitFlow(StagedFlow):
    id = "last_visit"
    title = "My last visit"
    description = "What the doctor said and found"
    icon = "notebook-pen"
    roles = frozenset({"patient"})
    group = "My health"
    keywords = ("last visit", "doctor say", "diagnosis", "visit summary", "last time")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        async with ctx.tool("my_record", "Reading your visit notes") as step:
            record = await ctx.parta.my_record(ctx.token)
            notes = _signed(record)
            step["summary"] = f"{len(notes)} signed notes"
        if not notes:
            ctx.paragraph("There are no signed visit notes on your record yet.")
            return None
        note = notes[0]
        names = await provider_names(ctx, [{"provider_id": note["provider_id"]}])
        ctx.paragraph(
            f"From your visit on {ctx.day(note['signed_at'])} with "
            f"{names.get(note['provider_id'], 'your doctor')}:"
        )
        if note["patient_summary"]:
            ctx.paragraph(f"> {note['patient_summary']}")
        ctx.card(
            "visit",
            "Visit summary",
            subtitle=ctx.day(note["signed_at"]),
            fields=[
                ("Reason", note["presenting_complaint"]),
                ("Diagnosis", "; ".join(d["description"] for d in note["diagnoses"])),
                ("Tests ordered", ", ".join(o["test_code"] for o in note["orders"])),
                ("Prescribed", ", ".join(p["medication"] for p in note["prescriptions"])),
            ],
        )
        ctx.suggest("My prescriptions", "Prepare for a test", "Book a follow-up")
        return None


class ReportsFlow(StagedFlow):
    id = "reports"
    title = "My reports"
    description = "Results you uploaded, and whether they're reviewed"
    icon = "file-text"
    roles = frozenset({"patient"})
    group = "My health"
    keywords = ("report", "result", "what does this mean", "uploaded")

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        async with ctx.tool("my_record", "Looking up your reports") as step:
            record = await ctx.parta.my_record(ctx.token)
            docs = sorted(record["documents"], key=lambda d: d["created_at"], reverse=True)[:8]
            step["summary"] = f"{len(docs)} reports"
        if not docs:
            ctx.paragraph(
                "You haven't uploaded any reports. Use **+ → Attach a report** below to add one."
            )
            data["stage"] = "done"
            return None
        data["patient_id"] = record["patient_id"]
        data["docs"] = {d["id"]: d["filename"] for d in docs}
        data["stage"] = "pick"
        return AskOptions(
            "Which report?",
            [
                Choice(
                    d["id"],
                    d["filename"],
                    f"Uploaded {ctx.day(d['created_at'])}",
                    {"extracted": "", "pending": "Processing"}.get(
                        d["extraction_status"], "No text"
                    ),
                )
                for d in docs
            ],
        )

    async def on_pick(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen not in data["docs"]:
            data["stage"] = "start"
            return None
        data["stage"] = "done"
        async with ctx.tool("read_report", f"Reading {data['docs'][chosen]}") as step:
            doc = await ctx.parta.document(ctx.token, data["patient_id"], chosen)
            step["summary"] = doc["extraction_status"]
        if not doc.get("extracted_text"):
            ctx.paragraph(
                "I can't read text from this file (it may be a scan or a photo). "
                "Your doctor will review it."
            )
            return None
        text = doc["extracted_text"].strip()
        ctx.paragraph("These are the values as printed on your report:")
        ctx.paragraph("```\n" + text[:1500] + ("\n…" if len(text) > 1500 else "") + "\n```")
        ctx.card(
            "document",
            doc["filename"],
            badge="Reviewed by your doctor" if doc["reviewed"] else "Not yet reviewed",
        )
        ctx.paragraph(
            "I can't interpret results or give a diagnosis — your doctor will "
            "review it and tell you what it means for you."
        )
        return None


# -- provider ----------------------------------------------------------------------------


async def my_provider_id(ctx: Ctx) -> str:
    providers = await ctx.parta.providers(ctx.token, limit=200)
    for p in providers:
        if p.get("user_id") == ctx.caller.user_id:
            return p["id"]
    raise FlowError("Your login isn't linked to a provider record, so I can't find your patients.")


class BriefFlow(StagedFlow):
    id = "brief"
    title = "Brief me on my next patient"
    description = "History, medication and open results, before they walk in"
    icon = "stethoscope"
    roles = frozenset({"provider"})
    group = "Patients"
    keywords = ("brief", "summarise my next", "summarize my next", "next patient")

    async def on_start(self, ctx: Ctx, data: dict, answer: dict | None):
        async with ctx.tool("my_schedule", "Looking up your next appointments") as step:
            pid = await my_provider_id(ctx)
            now = datetime.now(UTC)
            items = await ctx.parta.appointments(
                ctx.token,
                provider_id=pid,
                from_=(now - timedelta(hours=2)).isoformat(),
                to=(now + timedelta(days=7)).isoformat(),
            )
            items = sorted(
                (a for a in items if a["status"] in ("confirmed",) and a.get("starts_at")),
                key=lambda a: a["starts_at"],
            )[:6]
            step["summary"] = f"{len(items)} in the next week"
        if not items:
            ctx.paragraph("You have no confirmed appointments in the next week.")
            data["stage"] = "done"
            return None
        patients = await asyncio.gather(
            *[ctx.parta.patient(ctx.token, a["patient_id"]) for a in items], return_exceptions=True
        )
        data["appointments"] = {
            a["id"]: {
                "patient_id": a["patient_id"],
                "starts_at": a["starts_at"],
                "reason": a.get("reason") or "",
                "name": (
                    f"{p['first_name']} {p['last_name']}" if isinstance(p, dict) else "Patient"
                ),
            }
            for a, p in zip(items, patients, strict=True)
        }
        data["stage"] = "pick"
        return AskOptions(
            "Which patient?",
            [
                Choice(aid, a["name"], ctx.when(a["starts_at"]), "Next" if i == 0 else "")
                for i, (aid, a) in enumerate(data["appointments"].items())
            ],
        )

    async def on_pick(self, ctx: Ctx, data: dict, answer: dict | None):
        chosen = option_id(answer)
        if chosen not in data.get("appointments", {}):
            data["stage"] = "start"
            return None
        data["stage"] = "done"
        a = data["appointments"][chosen]
        try:
            async with ctx.tool("read_record", f"Reading {a['name']}'s record") as step:
                record = await ctx.parta.record(ctx.token, a["patient_id"])
                step["summary"] = (
                    f"{len(record['notes'])} notes, {len(record['orders'])} orders, "
                    f"{len(record['documents'])} documents"
                )
        except PartAError as exc:
            if exc.status in (403, 404):
                raise FlowError(
                    "You don't have a care relationship with this patient, so their "
                    "record isn't available to you."
                ) from None
            raise
        notes = _signed(record)
        last = notes[0] if notes else None
        since = last["signed_at"] if last else ""
        new_docs = [d for d in record["documents"] if d["created_at"] > since]
        open_orders = [o for o in record["orders"] if o["status"] != "reviewed"]
        active = [p for p in record["prescriptions"] if p["status"] == "active"]
        ctx.paragraph(
            f"**{a['name']}** — {ctx.when(a['starts_at'])}"
            + (f". Booked for: {a['reason']}" if a["reason"] else ".")
        )
        ctx.card(
            "brief",
            "Pre-visit brief",
            subtitle=a["name"],
            fields=[
                ("Last visit", ctx.day(last["signed_at"]) if last else "First visit"),
                (
                    "Last diagnosis",
                    "; ".join(d["description"] for d in last["diagnoses"]) if last else "",
                ),
                (
                    "Active medication",
                    "; ".join(f"{p['medication']} {p['dose']}" for p in active) or "None",
                ),
                (
                    "Open test orders",
                    "; ".join(
                        f"{o['test_name']} ({o['status'].replace('_', ' ')})" for o in open_orders
                    )
                    or "None",
                ),
                (
                    "New documents since last visit",
                    "; ".join(d["filename"] for d in new_docs) or "None",
                ),
            ],
        )
        if any(o["status"] == "result_uploaded" for o in open_orders):
            ctx.paragraph("There are results waiting for your review.")
            ctx.suggest("Results to review")
        return None


class ResultsToReviewFlow(StagedFlow):
    id = "results_review"
    title = "Results to review"
    description = "Uploaded results for tests you ordered"
    icon = "list-checks"
    roles = frozenset({"provider"})
    group = "Patients"
    keywords = ("review", "new results", "results")

    async def on_start(self, ctx: Ctx, data: dict[str, Any], answer: dict | None):
        data["stage"] = "done"
        async with ctx.tool("my_patients", "Checking your patients' test orders") as step:
            pid = await my_provider_id(ctx)
            now = datetime.now(UTC)
            items = await ctx.parta.appointments(
                ctx.token,
                provider_id=pid,
                from_=(now - timedelta(days=90)).isoformat(),
                to=(now + timedelta(days=30)).isoformat(),
            )
            patient_ids = list({a["patient_id"] for a in items if a.get("patient_id")})[:25]
            records = await asyncio.gather(
                *[ctx.parta.record(ctx.token, p) for p in patient_ids], return_exceptions=True
            )
            waiting = []
            for patient_id, record in zip(patient_ids, records, strict=True):
                if isinstance(record, BaseException):
                    continue
                for order in record["orders"]:
                    if order["status"] == "result_uploaded":
                        waiting.append((patient_id, order))
            step["summary"] = f"{len(waiting)} waiting"
        if not waiting:
            ctx.paragraph("No results are waiting for your review.")
            return None
        names = {}
        for patient_id in {p for p, _ in waiting}:
            try:
                p = await ctx.parta.patient(ctx.token, patient_id)
                names[patient_id] = f"{p['first_name']} {p['last_name']}"
            except PartAError:
                names[patient_id] = "Patient"
        ctx.paragraph(f"{len(waiting)} result{'s' if len(waiting) > 1 else ''} waiting for you:")
        for patient_id, order in waiting[:10]:
            ctx.card(
                "order",
                order["test_name"],
                subtitle=names[patient_id],
                badge="Result uploaded",
                fields=[("Ordered", ctx.day(order["created_at"]))],
            )
        return None
