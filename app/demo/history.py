"""Imported history: what MediNova's previous system would have handed over.

Two halves, deliberately separate:

- **planning** (`plan_day`) is pure: a random generator seeded with the date decides every
  slot, booking and outcome. A day's history is determined by its date and by the date it is
  imported (`fill_rate` trends with `today`); within one run, re-planning a day gives identical
  rows. The rules are unit-tested without a database;
- **importing** (`import_day`) writes one planned day: slots inserted with their final
  status, appointments marked `origin = 'legacy_import'`, visits, signed notes,
  prescriptions, test orders and result documents, billing, and the day's analytics -- no
  outbox events (no emails about the past), no workflows, no per-row audit.
"""

from __future__ import annotations

import random
import hashlib
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.content import CANCELLATION_REASONS, CONTENT, RESULT_TEXT
from app.demo.network import (
    SLOT_DURATION,
    PROVIDERS_BY_LICENCE,
    Network,
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    DemoProvider,
    is_child,
    session_starts,
    working,
)

from app.modules.analytics import service as analytics_service
from app.modules.billing.models import BillingRecord, BillingStatus
from app.modules.clinical.models import (
    ClinicalNote,
    NoteStatus,
    OrderStatus,
    PatientDocument,
    Prescription,
    PrescriptionStatus,
    TestOrder,
)
from app.modules.clinical.storage import DocumentStore
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
    Visit,
    VisitStatus,
)
from app.settings import Settings

ORIGIN = "legacy_import"
HISTORY_DAYS = 182
COMPLETED_SHARE = 0.82
CANCELLED_SHARE = 0.10  # the rest are no-shows
BUSY = frozenset({"General Practice", "Paediatrics", "Physiotherapy"})
LINKED_WEIGHT = 6  # the five patients with logins get rich histories


@dataclass(frozen=True)
class PlannedVisit:
    checked_in_at: datetime
    seen_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class PlannedAppointment:
    licence: str
    patient_index: int
    starts_at: datetime
    outcome: str  # "completed" | "cancelled" | "no_show"
    confirmed_at: datetime
    cancelled_at: datetime | None
    cancellation_reason: str | None
    visit: PlannedVisit | None
    case: int  # index into CONTENT[specialty]
    prescribe: bool
    order_test: bool


@dataclass(frozen=True)
class PlannedDay:
    day: date
    slots: tuple[tuple[str, datetime], ...]  # (licence, starts_at UTC)
    appointments: tuple[PlannedAppointment, ...]


def fill_rate(specialty: str, day: date, today: date) -> float:
    """Share of a session's slots booked: busier for GP, Paediatrics and Physiotherapy,
    Mondays up and Fridays down, and a slow rise towards today -- a trend worth charting."""
    rate = 0.66 + (0.08 if specialty in BUSY else 0.0)
    rate += {0: 0.04, 4: -0.04}.get(day.weekday(), 0.0)
    rate -= 0.012 * ((today - day).days / 30)
    return min(0.80, max(0.55, rate))


_ADULTS = tuple(i for i in range(PATIENT_COUNT) if not is_child(i))
_CHILDREN = tuple(i for i in range(PATIENT_COUNT) if is_child(i))


def plan_day(day: date, *, tz: ZoneInfo, today: date) -> PlannedDay:
    """Plan one day. Determined by `day` and `today` (the import date): the same pair always
    yields the same rows, but a different import date shifts the fill rate."""
    rng = random.Random(f"smarthealth-demo:{day.isoformat()}")
    used: set[int] = set()
    slots: list[tuple[str, datetime]] = []
    appointments: list[PlannedAppointment] = []
    for provider in PROVIDERS:
        if not working(provider, day):
            continue
        rate = fill_rate(provider.specialty, day, today)
        for position, start in enumerate(session_starts(day, tz)):
            slots.append((provider.licence, start))
            if rng.random() >= rate:
                continue
            pool = [i for i in (_CHILDREN if provider.specialty == "Paediatrics" else _ADULTS)
                    if i not in used]
            weights = [LINKED_WEIGHT if i in LINKED_PATIENT_INDEXES else 1 for i in pool]
            patient = rng.choices(pool, weights=weights)[0]
            used.add(patient)
            appointments.append(_outcome(rng, provider, patient, start, position))
    return PlannedDay(day, tuple(slots), tuple(appointments))


def _outcome(rng: random.Random, provider: DemoProvider, patient: int, start: datetime,
             position: int) -> PlannedAppointment:
    cases = CONTENT[provider.specialty]
    case = rng.randrange(len(cases))
    confirmed = start - timedelta(days=rng.randint(1, 21), minutes=rng.randint(0, 600))
    roll = rng.random()
    if roll < COMPLETED_SHARE:
        checked_in = start - timedelta(minutes=rng.randint(0, 12))
        wait = rng.randint(5, 25) + 3 * position  # the morning backs up
        seen = checked_in + timedelta(minutes=wait)
        completed = seen + timedelta(minutes=rng.randint(10, 25))
        return PlannedAppointment(
            provider.licence, patient, start, "completed", confirmed, None, None,
            PlannedVisit(checked_in, seen, completed), case,
            prescribe=bool(cases[case].medications) and rng.random() < 0.40,
            order_test=bool(cases[case].tests) and rng.random() < 0.25)
    if roll < COMPLETED_SHARE + CANCELLED_SHARE:
        cancelled = confirmed + (start - confirmed) * rng.uniform(0.3, 0.98)
        return PlannedAppointment(
            provider.licence, patient, start, "cancelled", confirmed, cancelled,
            rng.choice(CANCELLATION_REASONS), None, case, False, False)
    return PlannedAppointment(provider.licence, patient, start, "no_show", confirmed, None,
                              None, None, case, False, False)


def result_pdf(text: str) -> bytes:
    """A minimal one-page PDF carrying `text` -- enough for the document viewer and pypdf."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 11 Tf 50 780 Td ({safe}) Tj ET".encode("latin-1", "replace")
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1, xref)
    return bytes(out)


NO_SHOW_GRACE = timedelta(minutes=15)  # when the live visit timer would have marked it
REVIEW_AFTER = timedelta(days=14)
ACTOR = "system:demo-import"

_STATUS = {"completed": AppointmentStatus.COMPLETED, "cancelled": AppointmentStatus.CANCELLED,
           "no_show": AppointmentStatus.NO_SHOW}
# No-show is VOIDED on purpose (spec section 10): live `mark_no_show` never releases the
# authorisation hold, recorded as an open risk -- the import does not copy that behaviour.
_BILLING = {"completed": BillingStatus.CAPTURED, "cancelled": BillingStatus.VOIDED,
            "no_show": BillingStatus.VOIDED}


@dataclass
class DayCounts:
    slots: int = 0
    appointments: int = 0
    completed: int = 0
    cancelled: int = 0
    no_show: int = 0
    prescriptions: int = 0
    orders: int = 0
    documents: int = 0

    def add(self, other: DayCounts) -> None:
        for name, value in asdict(other).items():
            setattr(self, name, getattr(self, name) + value)


def _local_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


async def day_imported(session: AsyncSession, net: Network, day: date) -> bool:
    """A day is imported once a demo provider has a `legacy_import` appointment whose slot
    starts on it. Free slots alone prove nothing: `ensure_slots` gives upcoming days some, and
    those days become past."""
    lower, upper = _local_bounds(day, net.tz)
    found = await session.scalar(
        select(Appointment.id).join(ProviderSlot, Appointment.slot_id == ProviderSlot.id)
        .where(Appointment.origin == ORIGIN,
               ProviderSlot.provider_id.in_(net.provider_ids.values()),
               ProviderSlot.starts_at >= lower, ProviderSlot.starts_at < upper).limit(1))
    return found is not None


async def last_imported_day(session: AsyncSession, net: Network, *,
                            now: datetime) -> date | None:
    """The latest local day before `now`'s, holding a demo `legacy_import` appointment."""
    today_start, _ = _local_bounds(now.astimezone(net.tz).date(), net.tz)
    latest = await session.scalar(
        select(func.max(ProviderSlot.starts_at))
        .join(Appointment, Appointment.slot_id == ProviderSlot.id)
        .where(Appointment.origin == ORIGIN,
               ProviderSlot.provider_id.in_(net.provider_ids.values()),
               ProviderSlot.starts_at < today_start))
    return latest.astimezone(net.tz).date() if latest else None


def _utc_day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


async def _reconcile_with_existing(session: AsyncSession, net: Network,
                                   plan: PlannedDay) -> PlannedDay:
    """Make the day's plan fit what is already in the database. Never-booked slots of the
    day (from `ensure_slots`, or an earlier import of a day with no bookings) are deleted --
    nothing references them -- so the plan's own slots replace them. Slots a live booking
    holds stay; a planned slot overlapping one is dropped with its planned appointment, and so
    is a planned appointment whose patient already has a non-imported booking that overlaps
    it. A re-run therefore adds only what is missing."""
    lower, upper = _local_bounds(plan.day, net.tz)
    provider_ids = list(net.provider_ids.values())
    referenced = select(Appointment.id).where(Appointment.slot_id == ProviderSlot.id).exists()
    await session.execute(delete(ProviderSlot).where(
        ProviderSlot.provider_id.in_(provider_ids),
        ProviderSlot.starts_at >= lower, ProviderSlot.starts_at < upper, ~referenced))
    kept = (await session.execute(select(
        ProviderSlot.provider_id, ProviderSlot.starts_at, ProviderSlot.ends_at).where(
        ProviderSlot.provider_id.in_(provider_ids),
        ProviderSlot.starts_at >= lower - timedelta(hours=1),
        ProviderSlot.starts_at < upper))).all()
    by_provider: dict[uuid.UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    for provider_id, starts, ends in kept:
        by_provider[provider_id].append((starts, ends))

    def clashes(licence: str, start: datetime) -> bool:
        end = start + SLOT_DURATION
        return any(start < e and s < end for s, e in by_provider[net.provider_ids[licence]])

    dropped = {(licence, start) for licence, start in plan.slots if clashes(licence, start)}
    patient_ids = [net.patient_ids[a.patient_index] for a in plan.appointments]
    theirs = (await session.execute(
        select(Appointment.patient_id, ProviderSlot.starts_at, ProviderSlot.ends_at)
        .join(ProviderSlot, Appointment.slot_id == ProviderSlot.id)
        .where(Appointment.patient_id.in_(patient_ids),
               Appointment.origin.is_distinct_from(ORIGIN),
               ProviderSlot.starts_at >= lower - timedelta(hours=1),
               ProviderSlot.starts_at < upper))).all()
    busy: dict[uuid.UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    for patient_id, starts, ends in theirs:
        busy[patient_id].append((starts, ends))

    def patient_clash(a: PlannedAppointment) -> bool:
        end = a.starts_at + SLOT_DURATION
        return any(a.starts_at < e and s < end
                   for s, e in busy[net.patient_ids[a.patient_index]])

    appointments = tuple(a for a in plan.appointments
                         if (a.licence, a.starts_at) not in dropped and not patient_clash(a))
    slots = tuple(s for s in plan.slots if s not in dropped)
    return PlannedDay(plan.day, slots, appointments)


async def import_day(session: AsyncSession, net: Network, plan: PlannedDay, *,
                     store: DocumentStore, settings: Settings, now: datetime) -> DayCounts:
    """Write one planned day. Flushes, never commits: the caller commits per day, so a crash
    leaves whole days imported or not, and `day_imported` skips the finished ones."""
    counts = DayCounts()
    plan = await _reconcile_with_existing(session, net, plan)
    booked = {(a.licence, a.starts_at) for a in plan.appointments if a.outcome != "cancelled"}
    slots: dict[tuple[str, datetime], ProviderSlot] = {}
    for licence, start in plan.slots:
        meta = PROVIDERS_BY_LICENCE[licence]
        slot = ProviderSlot(
            provider_id=net.provider_ids[licence], clinic_id=net.clinic_ids[meta.clinic],
            department_id=net.department_ids[(meta.clinic, meta.specialty)],
            starts_at=start, ends_at=start + SLOT_DURATION,
            status=SlotStatus.BOOKED if (licence, start) in booked else SlotStatus.FREE)
        session.add(slot)
        slots[(licence, start)] = slot
    counts.slots = len(slots)
    await session.flush()

    rows: list[tuple[PlannedAppointment, Appointment]] = []
    for planned in plan.appointments:
        meta = PROVIDERS_BY_LICENCE[planned.licence]
        appointment = Appointment(
            patient_id=net.patient_ids[planned.patient_index],
            provider_id=net.provider_ids[planned.licence],
            clinic_id=net.clinic_ids[meta.clinic],
            department_id=net.department_ids[(meta.clinic, meta.specialty)],
            slot_id=slots[(planned.licence, planned.starts_at)].id,
            status=_STATUS[planned.outcome],
            reason=CONTENT[meta.specialty][planned.case].reason,
            origin=ORIGIN, confirmed_at=planned.confirmed_at,
            cancelled_at=planned.cancelled_at,
            cancellation_reason=planned.cancellation_reason,
            created_at=planned.confirmed_at)
        session.add(appointment)
        rows.append((planned, appointment))
    await session.flush()

    analytics: dict[tuple[date, uuid.UUID, uuid.UUID], Counter] = defaultdict(Counter)
    visits: list[tuple[PlannedAppointment, Appointment, Visit]] = []
    for planned, appointment in rows:
        key_ids = (appointment.clinic_id, appointment.provider_id)
        analytics[(_utc_day(planned.confirmed_at), *key_ids)]["booked"] += 1
        session.add(BillingRecord(
            appointment_id=appointment.id, patient_id=appointment.patient_id,
            status=_BILLING[planned.outcome], amount_minor=settings.billing_amount_minor,
            currency=settings.billing_currency, authorization_ref=f"legacy-{appointment.id}"))
        counts.appointments += 1
        if planned.outcome == "cancelled":
            counts.cancelled += 1
            analytics[(_utc_day(planned.cancelled_at), *key_ids)]["cancelled"] += 1
        elif planned.outcome == "no_show":
            counts.no_show += 1
            marked = planned.starts_at + SLOT_DURATION + NO_SHOW_GRACE
            analytics[(_utc_day(marked), *key_ids)]["no_show"] += 1
        else:
            counts.completed += 1
            v = planned.visit
            visit = Visit(appointment_id=appointment.id, checked_in_at=v.checked_in_at,
                          seen_at=v.seen_at, completed_at=v.completed_at,
                          status=VisitStatus.COMPLETED)
            session.add(visit)
            visits.append((planned, appointment, visit))
            day_key = (_utc_day(v.completed_at), *key_ids)
            analytics[day_key]["completed"] += 1
            analytics[day_key]["wait_seconds_sum"] += int((v.seen_at - v.checked_in_at)
                                                          .total_seconds())
            analytics[day_key]["wait_count"] += 1
    await session.flush()

    for planned, appointment, visit in visits:
        meta = PROVIDERS_BY_LICENCE[planned.licence]
        case = CONTENT[meta.specialty][planned.case]
        prescriptions = list(case.medications) if planned.prescribe else []
        tests = list(case.tests[:1]) if planned.order_test else []
        note = ClinicalNote(
            visit_id=visit.id, appointment_id=appointment.id,
            patient_id=appointment.patient_id, provider_id=appointment.provider_id,
            version=1, status=NoteStatus.SIGNED,
            presenting_complaint=case.complaint, findings=case.findings, clinician_notes="",
            patient_summary=case.summary,
            diagnoses=[{"description": d, "icd10": c} for d, c in case.diagnoses],
            draft_prescriptions=[{"medication": m.medication, "dose": m.dose,
                                  "frequency": m.frequency, "duration": f"{m.duration_days} days",
                                  "instructions": m.instructions} for m in prescriptions],
            draft_orders=[{"test_code": code, "reason": case.reason, "urgency": "routine"}
                          for code in tests],
            signed_at=planned.visit.completed_at - timedelta(minutes=2),
            signed_by=str(net.provider_user_ids[planned.licence]))
        session.add(note)
        await session.flush()
        completed_at = planned.visit.completed_at
        for m in prescriptions:
            still_taking = completed_at + timedelta(days=m.duration_days) > now
            session.add(Prescription(
                note_id=note.id, patient_id=appointment.patient_id, medication=m.medication,
                dose=m.dose, frequency=m.frequency, duration=f"{m.duration_days} days",
                instructions=m.instructions,
                status=PrescriptionStatus.ACTIVE if still_taking else PrescriptionStatus.COMPLETED))
            counts.prescriptions += 1
        for code in tests:
            reviewed = completed_at < now - REVIEW_AFTER
            order = TestOrder(
                note_id=note.id, patient_id=appointment.patient_id,
                ordered_by=appointment.provider_id, test_code=code, reason=case.reason,
                urgency="routine",
                status=OrderStatus.REVIEWED if reviewed else OrderStatus.ORDERED,
                reviewed_at=completed_at + timedelta(days=7) if reviewed else None)
            session.add(order)
            await session.flush()
            if reviewed:
                order.result_document_id = await _result_document(
                    session, store, appointment.patient_id, code, completed_at, order.id)
                counts.documents += 1
            counts.orders += 1
    await session.flush()

    for (day, clinic_id, provider_id), increments in analytics.items():
        await analytics_service.record(session, day=day, clinic_id=clinic_id,
                                       provider_id=provider_id, **dict(increments))
    return counts


async def _result_document(session: AsyncSession, store: DocumentStore, patient_id: uuid.UUID,
                           code: str, completed_at: datetime,
                           order_id: uuid.UUID) -> uuid.UUID:
    text = RESULT_TEXT[code]
    data = result_pdf(text)
    key = f"patients/{patient_id}/{uuid.uuid4()}"
    await store.put(key, data, "application/pdf")
    document = PatientDocument(
        patient_id=patient_id, uploaded_by=ACTOR, filename=f"{code}-result.pdf",
        media_type="application/pdf", size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(), object_key=key,
        test_order_id=order_id, extraction_status="extracted", extracted_text=text,
        created_at=completed_at + timedelta(days=3))
    session.add(document)
    await session.flush()
    return document.id
