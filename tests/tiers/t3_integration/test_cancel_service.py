"""Cancel and reschedule-completion against real Postgres: the paired write (appointment
and slot in one transaction, R-2), the refusals, and idempotency of the reschedule step.

Tests say which mutation of `service.py` they exist to kill.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import Clock
from app.core.errors import Conflict, NotFound
from app.core.pagination import PageParams
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.scheduling import service
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
    Visit,
)
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import (
    RecordingAudit,
    confirmed_appointment,
    seed_bookable,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]

ACTOR = "u-cancel"


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


async def _slot(factory, slot_id) -> ProviderSlot:
    async with factory() as session:
        return await session.get(ProviderSlot, slot_id)


async def _appointment(factory, appointment_id) -> Appointment:
    async with factory() as session:
        return await session.get(Appointment, appointment_id)


async def _cancel(factory, audit, appointment_id, reason="changed my mind") -> Appointment:
    async with factory() as session:
        appointment = await service.cancel_appointment(
            session, audit, appointment_id=appointment_id, reason=reason,
            now=Clock().now(), actor_user_id=ACTOR,
        )
        await session.commit()
        return appointment


async def _complete_reschedule(factory, audit, appointment_id):
    async with factory() as session:
        result = await service.complete_reschedule(
            session, audit, old_appointment_id=appointment_id, actor_user_id="system:wf"
        )
        await session.commit()
        return result


async def _check_in(factory, audit, appointment_id) -> None:
    async with factory() as session:
        await service.check_in(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id="u-1",
        )
        await session.commit()


async def _free_slots(factory, provider_id):
    async with factory() as session:
        slots, _ = await service.list_free_slots(
            session, provider_id=provider_id, now=Clock().now(), start=None, end=None,
            page=PageParams(limit=50, offset=0),
        )
    return [s.id for s in slots]


async def _pending(factory, audit, *, claimed: bool):
    async with factory() as session:
        b = await seed_bookable(session)
    appointment_id = uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=b.slot_id, reason=None, actor_user_id=None,
        )
        await session.commit()
    if claimed:
        async with factory() as session:
            await service.claim_slot(
                session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
                now=Clock().now(), actor_user_id=None,
            )
            await session.commit()
    return b, appointment_id


# --- cancel_appointment ---------------------------------------------------------------


async def test_cancelling_a_confirmed_appointment_frees_its_slot_in_one_write(factory):
    """The paired write. Kills: the slot UPDATE removed from `cancel_appointment`; the
    version increment dropped; and, via the rollback half, a slot write that escapes the
    appointment's transaction."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    booked_version = (await _slot(factory, b.slot_id)).version

    # Rolled back: neither half is visible.
    async with factory() as session:
        await service.cancel_appointment(
            session, audit, appointment_id=appointment_id, reason="x",
            now=Clock().now(), actor_user_id=ACTOR,
        )
        await session.rollback()
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.BOOKED

    returned = await _cancel(factory, audit, appointment_id, reason="moving away")

    appointment = await _appointment(factory, appointment_id)
    slot = await _slot(factory, b.slot_id)
    assert returned.id == appointment_id
    assert appointment.status is AppointmentStatus.CANCELLED
    assert appointment.cancelled_at is not None
    assert appointment.cancellation_reason == "moving away"
    assert appointment.slot_id == b.slot_id  # kept: the follow-up needs it
    assert slot.status is SlotStatus.FREE
    assert slot.version == booked_version + 1
    last = audit.events[-1]
    assert (last.action, last.actor_user_id) == ("cancelled", ACTOR)
    assert last.before["status"] == "confirmed" and last.after["status"] == "cancelled"


async def test_the_freed_slot_is_listed_as_free_again(factory):
    """R-2 from the reader's side: a released slot reappears in `list_free_slots`."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    assert await _free_slots(factory, b.provider_id) == []
    await _cancel(factory, audit, appointment_id)
    assert await _free_slots(factory, b.provider_id) == [b.slot_id]


async def test_cancelling_a_pending_appointment_that_holds_a_slot_releases_it(factory):
    """Kills: the release restricted to `booked` -- a `held` slot would stay held."""
    audit = RecordingAudit()
    b, appointment_id = await _pending(factory, audit, claimed=True)
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.HELD

    await _cancel(factory, audit, appointment_id)

    assert (await _slot(factory, b.slot_id)).status is SlotStatus.FREE
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CANCELLED


async def test_cancelling_an_unclaimed_pending_appointment_writes_no_slot(factory):
    audit = RecordingAudit()
    b, appointment_id = await _pending(factory, audit, claimed=False)
    before = await _slot(factory, b.slot_id)

    await _cancel(factory, audit, appointment_id)

    after = await _slot(factory, b.slot_id)
    assert (after.status, after.version) == (before.status, before.version)
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CANCELLED


async def test_a_booking_that_loses_to_a_cancel_compensates_as_a_no_op(factory):
    """The race in spec 4.3: after the cancel, `fail_booking` finds a non-pending
    appointment and leaves slot and appointment alone."""
    audit = RecordingAudit()
    b, appointment_id = await _pending(factory, audit, claimed=True)
    await _cancel(factory, audit, appointment_id)
    version = (await _slot(factory, b.slot_id)).version
    async with factory() as session:
        await service.fail_booking(
            session, audit, appointment_id=appointment_id, actor_user_id=None
        )
        await session.commit()
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CANCELLED
    assert (await _slot(factory, b.slot_id)).version == version


async def test_cancelling_twice_is_a_conflict_and_writes_nothing(factory):
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    await _cancel(factory, audit, appointment_id)
    events = len(audit.events)
    version = (await _slot(factory, b.slot_id)).version
    with pytest.raises(Conflict):
        await _cancel(factory, audit, appointment_id)
    assert len(audit.events) == events
    assert (await _slot(factory, b.slot_id)).version == version


async def test_a_checked_in_appointment_cannot_be_cancelled(factory):
    """Kills: the visit check dropped from `cancel_appointment`."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)
    with pytest.raises(Conflict, match="checked in"):
        await _cancel(factory, audit, appointment_id)
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.BOOKED


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW, AppointmentStatus.FAILED,
     AppointmentStatus.RESCHEDULED],
)
async def test_a_finished_appointment_cannot_be_cancelled(factory, status):
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    async with factory() as session:
        appointment = await session.get(Appointment, appointment_id)
        appointment.status = status
        await session.commit()
    with pytest.raises(Conflict):
        await _cancel(factory, audit, appointment_id)
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.BOOKED


async def test_cancelling_an_unknown_appointment_is_not_found(factory):
    with pytest.raises(NotFound):
        await _cancel(factory, RecordingAudit(), uuid.uuid4())


# --- complete_reschedule --------------------------------------------------------------


async def test_completing_a_reschedule_frees_the_old_slot_and_says_which(factory):
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    version = (await _slot(factory, b.slot_id)).version

    result = await _complete_reschedule(factory, audit, appointment_id)

    assert result.outcome == "rescheduled" and result.released_slot_id == b.slot_id
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.RESCHEDULED
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == version + 1
    assert audit.events[-1].action == "rescheduled"
    assert await _free_slots(factory, b.provider_id) == [b.slot_id]  # R-2, reader's side


async def test_completing_a_reschedule_twice_writes_once_and_answers_the_same(factory):
    """A retried activity: the same answer (including the slot, so the workflow still
    promotes the waitlist), no second slot write, no second audit entry."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    first = await _complete_reschedule(factory, audit, appointment_id)
    events = len(audit.events)
    version = (await _slot(factory, b.slot_id)).version

    second = await _complete_reschedule(factory, audit, appointment_id)

    assert second == first
    assert len(audit.events) == events
    assert (await _slot(factory, b.slot_id)).version == version


async def test_a_reschedule_over_a_cancelled_appointment_is_superseded(factory):
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    await _cancel(factory, audit, appointment_id)
    events = len(audit.events)
    version = (await _slot(factory, b.slot_id)).version

    result = await _complete_reschedule(factory, audit, appointment_id)

    assert result.outcome == "superseded" and result.released_slot_id is None
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CANCELLED
    assert len(audit.events) == events
    assert (await _slot(factory, b.slot_id)).version == version


async def test_a_reschedule_over_a_checked_in_appointment_is_superseded(factory):
    """Kills: the visit check removed from `complete_reschedule` -- a patient who has
    checked in would have their slot freed from under them."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)

    result = await _complete_reschedule(factory, audit, appointment_id)

    assert result.outcome == "superseded"
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.BOOKED
    async with factory() as session:
        assert await session.scalar(select(Visit).where(Visit.appointment_id == appointment_id))


async def test_completing_a_reschedule_of_an_unknown_appointment_is_not_found(factory):
    with pytest.raises(NotFound):
        await _complete_reschedule(factory, RecordingAudit(), uuid.uuid4())


# --- at most one live replacement (migration 0004, D-5) ----------------------------------


async def _replacement(factory, audit, b, old_id, *, starts_in):
    """A `pending` new appointment for `b`'s patient on a fresh slot, linked back to
    `old_id` -- what a reschedule's child booking inserts first."""
    async with factory() as session:
        slot_id = await scheduling_helpers.add_slot(session, b, starts_in=starts_in)
    new_id = uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=new_id, patient_id=b.patient_id,
            slot_id=slot_id, reason=None, actor_user_id=None, rescheduled_from_id=old_id,
        )
        await session.commit()
    return new_id


async def test_a_second_live_replacement_of_one_appointment_is_a_conflict(factory):
    """Two reschedules of one appointment, accepted together: the second booking's insert
    must fail permanently, so it compensates instead of confirming as well. Kills: the
    index dropped from migration 0004, and the `LIVE_RESCHEDULE_INDEX` mapping removed
    (the raw IntegrityError would be retried, not compensated)."""
    audit = RecordingAudit()
    b, old_id = await confirmed_appointment(factory, audit)
    first = await _replacement(factory, audit, b, old_id, starts_in=timedelta(days=2))
    events = len(audit.events)

    with pytest.raises(Conflict, match="already has a live replacement"):
        await _replacement(factory, audit, b, old_id, starts_in=timedelta(days=3))

    assert len(audit.events) == events
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(Appointment.id).where(Appointment.rescheduled_from_id == old_id)
            )
        )
    assert rows == [first]


async def test_a_failed_replacement_does_not_block_another(factory):
    """Partial on status, like `ux_appointments_live_slot`: a reschedule whose booking
    failed must not stop the patient trying again."""
    audit = RecordingAudit()
    b, old_id = await confirmed_appointment(factory, audit)
    first = await _replacement(factory, audit, b, old_id, starts_in=timedelta(days=2))
    async with factory() as session:
        await service.fail_booking(session, audit, appointment_id=first, actor_user_id=None)
        await session.commit()

    second = await _replacement(factory, audit, b, old_id, starts_in=timedelta(days=3))

    assert (await _appointment(factory, second)).rescheduled_from_id == old_id
