"""Booking operations. No FastAPI, no Temporal: activities and routers both call these.

Every function is idempotent, because a Temporal activity can run again after its
transaction committed but before the server heard it finished. Each detects that it has
already done its work and returns, without a second database write -- and, when the first
run *committed*, without a second audit entry. The entry goes to Mongo before the caller
commits, so a first run whose commit fails leaves an entry for a write that never landed,
and the retry that then makes the write records a second one (PRD R-1).

Every slot write increments `version` (PRD R-4) and happens in the same transaction as
the appointment write that justifies it (R-2). tests/meta/test_slot_writes.py keeps
slot-status updates inside this file.

The caller owns the transaction: these flush, the caller commits -- the Week 1 rule
(ARCHITECTURE §2.1), which is what lets an activity wrap one call in one unit of work.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound, Unprocessable
from app.db.constraints import violated_constraint
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
)
from app.modules.scheduling.state import require_transition

ENTITY = "appointment"

#: The partial unique index that allows one live appointment per slot. Named here
#: because a claim that loses to it must become a Conflict, not a 500.
LIVE_SLOT_INDEX = "ux_appointments_live_slot"

#: The appointment's patient FK. `create_pending` for an unknown patient violates it, and
#: that must be a permanent NotFound -- a raw IntegrityError is one Temporal retries.
PATIENT_FK = "fk_appointments_patient_id_patients"

#: The composite FK (migration 0002) tying `slot_id` to the appointment's own provider and
#: clinic. A claim naming another provider's or clinic's slot violates it.
SLOT_COHERENCE_FK = "fk_appointments_slot_provider_clinic"


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
    actor_user_id: str | None,
) -> None:
    """Insert the `pending` row, copying provider/clinic/department from the slot.

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
