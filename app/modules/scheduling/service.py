"""Booking and visit operations. No FastAPI, no Temporal: activities and routers both
call these.

Every function an activity calls is idempotent, because a Temporal activity can run again after its
transaction committed but before the server heard it finished. Each detects that it has
already done its work and returns, without a second database write -- and, when the first
run *committed*, without a second audit entry. The entry goes to Mongo before the caller
commits, so a first run whose commit fails leaves an entry for a write that never landed,
and the retry that then makes the write records a second one (PRD R-1). The three staff
visit actions are the exception, strict on purpose: only the HTTP endpoints call them,
nothing retries them, and a repeat is a human mistake to report.

Every slot write increments `version` (PRD R-4) and happens in the same transaction as
the appointment write that justifies it (R-2). tests/meta/test_slot_writes.py keeps
slot-status updates inside this file.

The caller owns the transaction: these flush, the caller commits -- the Week 1 rule
(ARCHITECTURE §2.1), which is what lets an activity wrap one call in one unit of work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.pagination import PageParams
from app.db.constraints import violated_constraint
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
    Visit,
    WaitlistEntry,
    WaitlistStatus,
)
from app.modules.scheduling.state import (
    VISIT_RESULT,
    NoShowOutcome,
    VisitAction,
    require_cancellable,
    require_transition,
)
from app.modules.scheduling.state import require_visit_action as check_visit_action

ENTITY = "appointment"
VISIT_ENTITY = "visit"
WAITLIST_ENTITY = "waitlist_entry"

#: The partial unique index that allows one live appointment per slot. Named here
#: because a claim that loses to it must become a Conflict, not a 500.
LIVE_SLOT_INDEX = "ux_appointments_live_slot"

#: The appointment's patient FK. `create_pending` for an unknown patient violates it, and
#: that must be a permanent NotFound -- a raw IntegrityError is one Temporal retries.
PATIENT_FK = "fk_appointments_patient_id_patients"

#: The composite FK (migration 0002) tying `slot_id` to the appointment's own provider and
#: clinic. A claim naming another provider's or clinic's slot violates it.
SLOT_COHERENCE_FK = "fk_appointments_slot_provider_clinic"

#: The waitlist's three foreign keys; an entry naming a missing patient, provider or
#: department is a permanent NotFound, not a 500.
WAITLIST_FKS = {
    "fk_waitlist_entries_patient_id_patients": "patient",
    "fk_waitlist_entries_provider_id_providers": "provider",
    "fk_waitlist_entries_department_id_departments": "department",
}


def _snapshot(appointment: Appointment) -> dict[str, object]:
    return {
        "status": appointment.status,
        "patient_id": str(appointment.patient_id),
        "slot_id": str(appointment.slot_id) if appointment.slot_id else None,
        "provider_id": str(appointment.provider_id),
        "clinic_id": str(appointment.clinic_id),
    }


async def get_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, *, for_update: bool = False
) -> Appointment | None:
    """Always a fresh read: `populate_existing` defeats the identity map, which could
    otherwise hand back a status another transaction has since changed."""
    statement = (
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _require(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    appointment = await get_appointment(session, appointment_id, for_update=True)
    if appointment is None:
        raise NotFound(f"appointment {appointment_id} does not exist")
    return appointment


async def _audit(
    audit: AuditLog, appointment: Appointment, action: str, actor_user_id: str | None,
    before: dict[str, object] | None,
) -> None:
    await audit.record(
        AuditEvent(
            entity_type=ENTITY, entity_id=str(appointment.id), action=action,
            actor_user_id=actor_user_id, before=before, after=_snapshot(appointment),
        )
    )


async def create_pending(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    patient_id: uuid.UUID, slot_id: uuid.UUID, reason: str | None,
    actor_user_id: str | None, rescheduled_from_id: uuid.UUID | None = None,
) -> None:
    """Insert the `pending` row, copying provider/clinic/department from the slot.
    `rescheduled_from_id` is set only for the new appointment of a reschedule.

    `ON CONFLICT (id) DO NOTHING` makes a retry harmless; only the call that actually
    inserted writes the audit entry. An existing row is a retry only if it describes the
    same booking: the id reused for another patient, provider or clinic is a Conflict, not
    a silent success that lets the workflow go on to book someone else's row.
    """
    slot = await session.get(ProviderSlot, slot_id)
    if slot is None:
        raise NotFound(f"slot {slot_id} does not exist")
    try:
        result = await session.execute(
            insert(Appointment)
            .values(
                id=appointment_id, patient_id=patient_id, provider_id=slot.provider_id,
                clinic_id=slot.clinic_id, department_id=slot.department_id, reason=reason,
                rescheduled_from_id=rescheduled_from_id,
            )
            .on_conflict_do_nothing(index_elements=[Appointment.id])
        )
    except IntegrityError as exc:
        if violated_constraint(exc) == PATIENT_FK:
            raise NotFound(f"patient {patient_id} does not exist") from exc
        raise
    if result.rowcount == 0:
        existing = await _require(session, appointment_id)
        if (
            existing.patient_id != patient_id
            or existing.provider_id != slot.provider_id
            or existing.clinic_id != slot.clinic_id
        ):
            raise Conflict(f"appointment id {appointment_id} reused for a different booking")
        return  # the same booking: a retried activity
    if result.rowcount == 1:
        appointment = await _require(session, appointment_id)
        await _audit(audit, appointment, "booking_requested", actor_user_id, None)


async def ensure_slot_bookable(
    session: AsyncSession, *, slot_id: uuid.UUID, now: datetime
) -> None:
    """R-3: a slot that has started cannot be booked. Postgres cannot enforce this --
    CHECK conditions must be immutable, and now() is not."""
    slot = await session.get(ProviderSlot, slot_id)
    if slot is None:
        raise NotFound(f"slot {slot_id} does not exist")
    if slot.starts_at <= now:
        raise Unprocessable(f"slot {slot_id} started at {slot.starts_at.isoformat()}")


async def claim_slot(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    slot_id: uuid.UUID, now: datetime, actor_user_id: str | None,
) -> None:
    """The conditional claim (PRD D-2). Zero rows updated *is* the conflict.

    Only a `pending` appointment claims, and it claims once: one already holding a
    different slot is a Conflict, because overwriting `slot_id` would leave the first slot
    `held` with nothing left to release it. The UPDATE re-checks R-3 itself --
    `ensure_slot_bookable` ran in an earlier transaction, and a retry or a backlogged
    worker can arrive here after the slot has started.

    The partial unique index `ux_appointments_live_slot` is the second gate: if the
    UPDATE were ever wrong, attaching `slot_id` to a second live appointment still fails.
    """
    appointment = await _require(session, appointment_id)
    if appointment.status is not AppointmentStatus.PENDING:
        raise Conflict(f"appointment {appointment_id} is {appointment.status.value}")
    if appointment.slot_id == slot_id:
        return  # already claimed by this booking: a retried activity
    if appointment.slot_id is not None:
        raise Conflict(f"appointment {appointment_id} already holds slot {appointment.slot_id}")
    before = _snapshot(appointment)

    claimed = await session.scalar(
        update(ProviderSlot)
        .where(
            ProviderSlot.id == slot_id,
            ProviderSlot.status == SlotStatus.FREE,
            ProviderSlot.starts_at > now,
        )
        .values(status=SlotStatus.HELD, version=ProviderSlot.version + 1)
        .returning(ProviderSlot.id)
    )
    if claimed is None:
        # Zero rows says only that the claim lost; re-read the slot to say why.
        slot = await session.scalar(
            select(ProviderSlot)
            .where(ProviderSlot.id == slot_id)
            .execution_options(populate_existing=True)
        )
        if slot is None:
            raise NotFound(f"slot {slot_id} does not exist")
        if slot.starts_at <= now:
            raise Unprocessable(f"slot {slot_id} started at {slot.starts_at.isoformat()}")
        raise Conflict(f"slot {slot_id} is no longer free")

    appointment.slot_id = slot_id
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = violated_constraint(exc)
        if constraint == LIVE_SLOT_INDEX:
            raise Conflict(f"slot {slot_id} is held by another appointment") from exc
        if constraint == SLOT_COHERENCE_FK:
            raise Conflict(
                f"slot {slot_id} belongs to another provider or clinic than "
                f"appointment {appointment_id}"
            ) from exc
        raise
    await _audit(audit, appointment, "slot_claimed", actor_user_id, before)


async def confirm_booking(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    now: datetime, actor_user_id: str | None,
) -> None:
    """pending -> confirmed and held -> booked, in one transaction."""
    appointment = await _require(session, appointment_id)
    if appointment.status is AppointmentStatus.CONFIRMED:
        return
    require_transition(appointment.status, AppointmentStatus.CONFIRMED)
    if appointment.slot_id is None:
        raise Conflict(f"appointment {appointment_id} holds no slot")
    before = _snapshot(appointment)

    booked = await session.scalar(
        update(ProviderSlot)
        .where(ProviderSlot.id == appointment.slot_id, ProviderSlot.status == SlotStatus.HELD)
        .values(status=SlotStatus.BOOKED, version=ProviderSlot.version + 1)
        .returning(ProviderSlot.id)
    )
    if booked is None:
        raise Conflict(f"slot {appointment.slot_id} is not held")
    appointment.status = AppointmentStatus.CONFIRMED
    appointment.confirmed_at = now
    await session.flush()
    await _audit(audit, appointment, "confirmed", actor_user_id, before)


async def fail_booking(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    actor_user_id: str | None,
) -> None:
    """Compensation: appointment -> failed, and its held slot -> free, together.

    A no-op when there is nothing to undo: no row (create_pending never ran), already
    failed (a retry), or no longer pending (something else, e.g. a cancellation, owns the
    appointment now and has already dealt with the slot). The slot is released only if
    *this* appointment holds it, so the loser of a race never frees the winner's slot.

    A failed appointment keeps its `slot_id`. That is harmless for the partial index, which
    ignores failed rows, but the FK's `ON DELETE RESTRICT` then blocks deleting a slot that
    has failed bookings -- slot regeneration must account for it.
    """
    appointment = await get_appointment(session, appointment_id, for_update=True)
    if appointment is None or appointment.status is not AppointmentStatus.PENDING:
        return
    before = _snapshot(appointment)
    if appointment.slot_id is not None:
        await session.execute(
            update(ProviderSlot)
            .where(
                ProviderSlot.id == appointment.slot_id,
                ProviderSlot.status == SlotStatus.HELD,
            )
            .values(status=SlotStatus.FREE, version=ProviderSlot.version + 1)
        )
    require_transition(appointment.status, AppointmentStatus.FAILED)
    appointment.status = AppointmentStatus.FAILED
    await session.flush()
    await _audit(audit, appointment, "booking_failed", actor_user_id, before)


# --- visit lifecycle (spec 4.2) -------------------------------------------------------
#
# The database records human actions synchronously; the visit workflow owns time (D-12).
# `check_in`, `mark_seen` and `complete_visit` are called by the endpoints in the
# request's transaction and are strict: an illegal action -- a repeat included -- raises
# Conflict, because nothing retries them and a silent no-op would tell staff a second
# check-in "worked". `mark_no_show` is the timer's activity, and idempotent like booking.
# All of them lock the appointment row first (`_require`), so a check-in and the no-show
# timer serialise on it. None writes a slot: a completed or no-show appointment's slot is
# never freed (normally already in the past) and stays `booked` (spec 2, the R-2 table).


def _visit_snapshot(visit: Visit) -> dict[str, object]:
    return {
        "appointment_id": str(visit.appointment_id),
        "status": visit.status,
        "checked_in_at": visit.checked_in_at.isoformat(),
        "seen_at": visit.seen_at.isoformat() if visit.seen_at else None,
        "completed_at": visit.completed_at.isoformat() if visit.completed_at else None,
    }


async def _audit_visit(
    audit: AuditLog, visit: Visit, action: str, actor_user_id: str | None,
    before: dict[str, object] | None,
) -> None:
    await audit.record(
        AuditEvent(
            entity_type=VISIT_ENTITY, entity_id=str(visit.id), action=action,
            actor_user_id=actor_user_id, before=before, after=_visit_snapshot(visit),
        )
    )


async def get_visit(
    session: AsyncSession, appointment_id: uuid.UUID, *, for_update: bool = False
) -> Visit | None:
    """The appointment's visit, or None. A fresh read, for the reason `get_appointment`
    is."""
    statement = (
        select(Visit)
        .where(Visit.appointment_id == appointment_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def no_show_deadline(
    session: AsyncSession, *, appointment_id: uuid.UUID, grace: timedelta
) -> datetime:
    """When the appointment becomes a no-show if nobody has checked in: the slot's end
    plus `grace`. The workflow turns this into a timer; reading it here keeps the slot
    lookup in the service."""
    appointment = await get_appointment(session, appointment_id)
    if appointment is None:
        raise NotFound(f"appointment {appointment_id} does not exist")
    slot = await session.get(ProviderSlot, appointment.slot_id) if appointment.slot_id else None
    if slot is None:
        raise NotFound(f"appointment {appointment_id} holds no slot")
    return slot.ends_at + grace


async def check_in(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID, now: datetime,
    actor_user_id: str | None,
) -> Visit:
    """Insert the visit row (`checked_in`). Strict: a visit that already exists is a
    Conflict, as is an appointment that is not `confirmed` -- including one the no-show
    timer has already closed.

    Holds the appointment row lock until the caller commits; `mark_no_show` waits on it,
    which is what makes a check-in near the deadline win or lose cleanly. `ON CONFLICT DO
    NOTHING` is a second gate behind the check: if anything ever inserted without the
    lock, the unique `appointment_id` would still allow one visit, and zero rows is a
    Conflict too."""
    appointment = await _require(session, appointment_id)
    existing = await get_visit(session, appointment_id, for_update=True)
    check_visit_action(
        VisitAction.CHECK_IN, appointment.status, existing.status if existing else None
    )
    result = await session.execute(
        insert(Visit)
        .values(
            appointment_id=appointment_id, checked_in_at=now,
            status=VISIT_RESULT[VisitAction.CHECK_IN],
        )
        .on_conflict_do_nothing(index_elements=[Visit.appointment_id])
    )
    if result.rowcount != 1:
        raise Conflict(f"appointment {appointment_id} is already checked in")
    visit = await get_visit(session, appointment_id)
    await _audit_visit(audit, visit, "checked_in", actor_user_id, None)
    return visit


async def mark_seen(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID, now: datetime,
    actor_user_id: str | None,
) -> Visit:
    """checked_in -> in_progress. Strict: anything else, a second `seen` included, is a
    Conflict."""
    appointment = await _require(session, appointment_id)
    visit = await get_visit(session, appointment_id, for_update=True)
    check_visit_action(VisitAction.SEEN, appointment.status, visit.status if visit else None)
    before = _visit_snapshot(visit)
    visit.seen_at = now
    visit.status = VISIT_RESULT[VisitAction.SEEN]
    await session.flush()
    await _audit_visit(audit, visit, "seen", actor_user_id, before)
    return visit


async def complete_visit(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID, now: datetime,
    actor_user_id: str | None,
) -> Visit:
    """in_progress -> completed, and the appointment confirmed -> completed, together.
    Strict: completing twice is a Conflict (the appointment is no longer `confirmed`).

    No slot write: completion never frees a slot (normally already in the past) (R-2)."""
    appointment = await _require(session, appointment_id)
    visit = await get_visit(session, appointment_id, for_update=True)
    check_visit_action(VisitAction.COMPLETE, appointment.status, visit.status if visit else None)
    require_transition(appointment.status, AppointmentStatus.COMPLETED)
    visit_before = _visit_snapshot(visit)
    appointment_before = _snapshot(appointment)
    visit.completed_at = now
    visit.status = VISIT_RESULT[VisitAction.COMPLETE]
    appointment.status = AppointmentStatus.COMPLETED
    await session.flush()
    await _audit_visit(audit, visit, "completed", actor_user_id, visit_before)
    await _audit(audit, appointment, "completed", actor_user_id, appointment_before)
    return visit


async def mark_no_show(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    actor_user_id: str | None,
) -> NoShowOutcome:
    """confirmed -> no_show if nobody checked in. Idempotent, and writes only for
    `NO_SHOW` on its first run:

    - `NO_SHOW`: marked now, or already was (a retried activity);
    - `VISIT_EXISTS`: a check-in reached the database first -- not a no-show, whatever
      the workflow's flags say, because its signal may have been lost or still be in
      flight;
    - `CLOSED`: the appointment left `confirmed` (cancelled, rescheduled, completed).

    The timer fires at a fixed time while staff act on their own; this is where the race
    is settled. `_require` takes the appointment row lock that `check_in` also takes, so a
    check-in still uncommitted makes this wait for it, and the visit read below then sees
    the committed row (READ COMMITTED reads per statement). No slot write (R-2)."""
    appointment = await _require(session, appointment_id)
    if appointment.status is AppointmentStatus.NO_SHOW:
        return NoShowOutcome.NO_SHOW
    if appointment.status is not AppointmentStatus.CONFIRMED:
        return NoShowOutcome.CLOSED
    if await get_visit(session, appointment_id) is not None:
        return NoShowOutcome.VISIT_EXISTS
    require_transition(appointment.status, AppointmentStatus.NO_SHOW)
    before = _snapshot(appointment)
    appointment.status = AppointmentStatus.NO_SHOW
    await session.flush()
    await _audit(audit, appointment, "no_show", actor_user_id, before)
    return NoShowOutcome.NO_SHOW


# --- cancel and reschedule (spec 4.3, 4.4) ---------------------------------------------
#
# Releasing a slot is a paired write (R-2): the appointment leaves its live status and the
# slot returns to `free` in one transaction, under the appointment's row lock. A cancelled
# or rescheduled appointment keeps its `slot_id`, as a failed one does, so the follow-up
# workflow can say which slot to offer the waitlist.


@dataclass(frozen=True)
class RescheduleResult:
    """What `complete_reschedule` did. `released_slot_id` is the old appointment's slot
    when it was (or already had been) released, else None -- the workflow offers exactly
    that slot to the waitlist."""

    outcome: Literal["rescheduled", "superseded"]
    released_slot_id: uuid.UUID | None


async def _release_slot(session: AsyncSession, slot_id: uuid.UUID) -> None:
    await session.execute(
        update(ProviderSlot)
        .where(
            ProviderSlot.id == slot_id,
            ProviderSlot.status.in_((SlotStatus.HELD, SlotStatus.BOOKED)),
        )
        .values(status=SlotStatus.FREE, version=ProviderSlot.version + 1)
    )


async def cancel_appointment(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID, reason: str,
    now: datetime, actor_user_id: str | None,
) -> Appointment:
    """pending|confirmed -> cancelled, and the slot it holds -> free, together.

    Strict, like the visit actions: only the endpoint calls it and nothing retries it, so
    cancelling a second time is a Conflict, not a silent success. A visit blocks it -- a
    checked-in patient is not cancelled.

    A `pending` appointment may hold a `held` slot (released here) or none yet, when the
    booking has not claimed one (nothing to release). Either way its booking then finds
    the appointment no longer `pending`: the next step fails permanently and
    `fail_booking` leaves it alone (spec 4.3).

    The audit entry goes to Mongo before the caller commits (PRD R-1), as everywhere."""
    appointment = await _require(session, appointment_id)
    visit = await get_visit(session, appointment_id)
    require_cancellable(appointment.status, visit is not None)
    require_transition(appointment.status, AppointmentStatus.CANCELLED)
    before = _snapshot(appointment)
    if appointment.slot_id is not None:
        await _release_slot(session, appointment.slot_id)
    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = now
    appointment.cancellation_reason = reason
    await session.flush()
    await _audit(audit, appointment, "cancelled", actor_user_id, before)
    return appointment


async def complete_reschedule(
    session: AsyncSession, audit: AuditLog, *, old_appointment_id: uuid.UUID,
    actor_user_id: str | None,
) -> RescheduleResult:
    """Move the old appointment out of the way once the new one is confirmed:
    confirmed -> rescheduled, and its slot -> free, together. Idempotent -- it is an
    activity -- and writes (and audits) only on its first run:

    - already `rescheduled`: a retried activity; answers the same, writes nothing;
    - not `confirmed`, or a visit exists: the old appointment was cancelled or the
      patient checked in while the new booking ran. Nothing is released and the
      reschedule is `superseded`; the new booking stands.

    A first run whose commit fails leaves an audit entry for a write that never landed,
    and its retry records a second (PRD R-1)."""
    appointment = await _require(session, old_appointment_id)
    if appointment.status is AppointmentStatus.RESCHEDULED:
        return RescheduleResult("rescheduled", appointment.slot_id)
    if (
        appointment.status is not AppointmentStatus.CONFIRMED
        or await get_visit(session, old_appointment_id) is not None
    ):
        return RescheduleResult("superseded", None)
    require_transition(appointment.status, AppointmentStatus.RESCHEDULED)
    before = _snapshot(appointment)
    if appointment.slot_id is not None:
        await _release_slot(session, appointment.slot_id)
    appointment.status = AppointmentStatus.RESCHEDULED
    await session.flush()
    await _audit(audit, appointment, "rescheduled", actor_user_id, before)
    return RescheduleResult("rescheduled", appointment.slot_id)


# --- waitlist (spec 4.5) ---------------------------------------------------------------


def _waitlist_snapshot(entry: WaitlistEntry) -> dict[str, object]:
    return {
        "status": entry.status,
        "patient_id": str(entry.patient_id),
        "provider_id": str(entry.provider_id) if entry.provider_id else None,
        "department_id": str(entry.department_id) if entry.department_id else None,
        "window_start": entry.window_start.isoformat(),
        "window_end": entry.window_end.isoformat(),
    }


async def _audit_waitlist(
    audit: AuditLog, entry: WaitlistEntry, action: str, actor_user_id: str | None,
    before: dict[str, object] | None,
) -> None:
    await audit.record(
        AuditEvent(
            entity_type=WAITLIST_ENTITY, entity_id=str(entry.id), action=action,
            actor_user_id=actor_user_id, before=before, after=_waitlist_snapshot(entry),
        )
    )


async def add_waitlist_entry(
    session: AsyncSession, audit: AuditLog, *, patient_id: uuid.UUID,
    provider_id: uuid.UUID | None, department_id: uuid.UUID | None,
    window_start: datetime, window_end: datetime, actor_user_id: str | None,
) -> WaitlistEntry:
    """A new `active` entry. The window must move forward; an unknown patient, provider
    or department is a NotFound."""
    if window_end <= window_start:
        raise Unprocessable("window_end must be after window_start")
    entry = WaitlistEntry(
        patient_id=patient_id, provider_id=provider_id, department_id=department_id,
        window_start=window_start, window_end=window_end,
    )
    session.add(entry)
    try:
        await session.flush()
    except IntegrityError as exc:
        missing = WAITLIST_FKS.get(violated_constraint(exc) or "")
        if missing is not None:
            raise NotFound(f"{missing} does not exist") from exc
        raise
    await session.refresh(entry)  # status and timestamps are server defaults
    await _audit_waitlist(audit, entry, "joined", actor_user_id, None)
    return entry


async def list_waitlist(
    session: AsyncSession, *, patient_id: uuid.UUID | None, page: PageParams
) -> tuple[list[WaitlistEntry], int]:
    """Entries, oldest first, optionally one patient's."""
    conditions = [WaitlistEntry.patient_id == patient_id] if patient_id is not None else []
    total = await session.scalar(
        select(func.count()).select_from(WaitlistEntry).where(*conditions)
    )
    rows = await session.scalars(
        select(WaitlistEntry)
        .where(*conditions)
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)


async def waitlist_candidates(
    session: AsyncSession, *, slot_id: uuid.UUID, limit: int = 3
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """`(entry_id, patient_id)` of up to `limit` entries to offer a released slot, oldest
    first: `active`, window containing the slot's start (`window_start <= starts_at <
    window_end`), and provider/department matching the slot's wherever the entry names
    one. The patient who just released the slot is not excluded -- their window decides.

    Read-only and not locked: two promotions may name the same entry. The workflow's
    child-booking id, not a row lock, is what keeps one entry from being booked twice."""
    slot = await session.get(ProviderSlot, slot_id)
    if slot is None:
        return []
    rows = await session.execute(
        select(WaitlistEntry.id, WaitlistEntry.patient_id)
        .where(
            WaitlistEntry.status == WaitlistStatus.ACTIVE,
            WaitlistEntry.window_start <= slot.starts_at,
            WaitlistEntry.window_end > slot.starts_at,
            or_(
                WaitlistEntry.provider_id.is_(None),
                WaitlistEntry.provider_id == slot.provider_id,
            ),
            or_(
                WaitlistEntry.department_id.is_(None),
                WaitlistEntry.department_id == slot.department_id,
            ),
        )
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        .limit(limit)
    )
    return [(row.id, row.patient_id) for row in rows]


async def fulfil_waitlist_entry(
    session: AsyncSession, audit: AuditLog, *, entry_id: uuid.UUID,
    actor_user_id: str | None,
) -> None:
    """active -> fulfilled. Idempotent: an already-fulfilled entry is a retried activity
    and writes nothing. A cancelled or expired one is a Conflict."""
    entry = await session.scalar(
        select(WaitlistEntry)
        .where(WaitlistEntry.id == entry_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if entry is None:
        raise NotFound(f"waitlist entry {entry_id} does not exist")
    if entry.status is WaitlistStatus.FULFILLED:
        return
    if entry.status is not WaitlistStatus.ACTIVE:
        raise Conflict(f"waitlist entry {entry_id} is {entry.status.value}")
    before = _waitlist_snapshot(entry)
    entry.status = WaitlistStatus.FULFILLED
    await session.flush()
    await _audit_waitlist(audit, entry, "fulfilled", actor_user_id, before)


async def list_free_slots(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID,
    now: datetime,
    start: datetime | None,
    end: datetime | None,
    page: PageParams,
) -> tuple[list[ProviderSlot], int]:
    """Free slots of one provider that have not started, earliest first.

    The same two conditions `claim_slot` enforces -- `status = 'free'` and
    `starts_at > now` -- so every listed slot is one a booking could take. It can still
    lose a race to another booking; the claim, not this list, is the authority.
    """
    lower = max(now, start) if start is not None else now
    conditions = [
        ProviderSlot.provider_id == provider_id,
        ProviderSlot.status == SlotStatus.FREE,
        ProviderSlot.starts_at > lower,
    ]
    if end is not None:
        conditions.append(ProviderSlot.starts_at < end)
    total = await session.scalar(
        select(func.count()).select_from(ProviderSlot).where(*conditions)
    )
    rows = await session.scalars(
        select(ProviderSlot)
        .where(*conditions)
        .order_by(ProviderSlot.starts_at, ProviderSlot.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)
