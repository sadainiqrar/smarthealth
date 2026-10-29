"""One patient, one place at a time: `claim_slot` refuses a slot that overlaps another
live appointment of the same patient (PART-A-FR-2, PRD R-23).

The slot-level gates (the conditional UPDATE, `ux_appointments_live_slot`, the GiST
exclusion) all reason about one *provider's* time. None of them sees a patient holding
two providers at once, so these tests always use two providers -- the case every other
gate waves through.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import Clock
from app.core.errors import Conflict
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.scheduling import service
from app.modules.scheduling.models import Appointment, ProviderSlot, SlotStatus
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


def _tomorrow_at(hour: int, minute: int = 0) -> datetime:
    day = datetime.now(UTC).date() + timedelta(days=2)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


async def _two_providers(factory, first_at: datetime, second_at: datetime):
    """One patient, and a slot at each of two providers. Returns both bookables; the
    second carries the first's patient."""
    async with factory() as session:
        first = await seed_bookable(session, starts_at=first_at)
        second = await seed_bookable(session, starts_at=second_at, patient_id=first.patient_id)
    return first, second


async def _pending(factory, audit, patient_id, slot_id, *, rescheduled_from_id=None):
    appointment_id = uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=patient_id,
            slot_id=slot_id, reason=None, actor_user_id=None,
            rescheduled_from_id=rescheduled_from_id,
        )
        await session.commit()
    return appointment_id


async def _claim(factory, audit, appointment_id, slot_id) -> None:
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=slot_id,
            now=Clock().now(), actor_user_id=None,
        )
        await session.commit()


async def _confirm(factory, audit, appointment_id) -> None:
    async with factory() as session:
        await service.confirm_booking(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id=None,
        )
        await session.commit()


async def _book(factory, audit, patient_id, slot_id) -> uuid.UUID:
    appointment_id = await _pending(factory, audit, patient_id, slot_id)
    await _claim(factory, audit, appointment_id, slot_id)
    await _confirm(factory, audit, appointment_id)
    return appointment_id


async def _slot(factory, slot_id) -> ProviderSlot:
    async with factory() as session:
        return await session.get(ProviderSlot, slot_id)


async def _appointment(factory, appointment_id) -> Appointment:
    async with factory() as session:
        return await session.get(Appointment, appointment_id)


async def test_an_overlapping_claim_for_the_same_patient_is_a_conflict(factory):
    """10:00-10:30 with one provider, 10:15-10:45 with another. Kills: the overlap check
    removed (the claim would succeed and the slot go `held`)."""
    first, second = await _two_providers(factory, _tomorrow_at(10), _tomorrow_at(10, 15))
    audit = RecordingAudit()
    await _book(factory, audit, first.patient_id, first.slot_id)
    late = await _pending(factory, audit, first.patient_id, second.slot_id)

    with pytest.raises(Conflict, match="overlaps"):
        await _claim(factory, audit, late, second.slot_id)
    slot = await _slot(factory, second.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 0
    assert (await _appointment(factory, late)).slot_id is None


async def test_a_pending_appointment_that_holds_a_slot_blocks_an_overlap(factory):
    """`pending` with a slot is live too: a booking mid-workflow already owns its time.
    Kills: the live-status filter narrowed to `confirmed`."""
    first, second = await _two_providers(factory, _tomorrow_at(11), _tomorrow_at(11))
    audit = RecordingAudit()
    holding = await _pending(factory, audit, first.patient_id, first.slot_id)
    await _claim(factory, audit, holding, first.slot_id)
    late = await _pending(factory, audit, first.patient_id, second.slot_id)

    with pytest.raises(Conflict, match="overlaps"):
        await _claim(factory, audit, late, second.slot_id)
    assert (await _slot(factory, second.slot_id)).status is SlotStatus.FREE


async def test_adjacent_slots_are_not_an_overlap(factory):
    """Slots are half-open, `[starts_at, ends_at)`: 10:00-10:30 then 10:30-11:00 is two
    consecutive visits, not a clash -- on either side of the booked one. Kills: `<=` or
    `>=` in either half of the overlap predicate."""
    first, after = await _two_providers(factory, _tomorrow_at(12), _tomorrow_at(12, 30))
    async with factory() as session:
        before = await seed_bookable(
            session, starts_at=_tomorrow_at(11, 30), patient_id=first.patient_id
        )
    audit = RecordingAudit()
    await _book(factory, audit, first.patient_id, first.slot_id)
    await _book(factory, audit, first.patient_id, after.slot_id)
    await _book(factory, audit, first.patient_id, before.slot_id)

    assert (await _slot(factory, after.slot_id)).status is SlotStatus.BOOKED
    assert (await _slot(factory, before.slot_id)).status is SlotStatus.BOOKED


async def test_a_cancelled_appointment_does_not_block_its_time(factory):
    """Only a live appointment occupies the patient. Kills: the status filter removed."""
    first, second = await _two_providers(factory, _tomorrow_at(13), _tomorrow_at(13, 15))
    audit = RecordingAudit()
    old = await _book(factory, audit, first.patient_id, first.slot_id)
    async with factory() as session:
        await service.cancel_appointment(
            session, audit, appointment_id=old, reason="moved", now=Clock().now(),
            actor_user_id=None,
        )
        await session.commit()

    await _book(factory, audit, first.patient_id, second.slot_id)
    assert (await _slot(factory, second.slot_id)).status is SlotStatus.BOOKED


async def test_another_patient_is_not_blocked(factory):
    """The check is per patient. Kills: the patient predicate removed."""
    async with factory() as session:
        first = await seed_bookable(session, starts_at=_tomorrow_at(14))
        other = await seed_bookable(session, starts_at=_tomorrow_at(14))
    audit = RecordingAudit()
    await _book(factory, audit, first.patient_id, first.slot_id)
    await _book(factory, audit, other.patient_id, other.slot_id)


async def test_a_reschedule_may_overlap_the_appointment_it_replaces(factory):
    """D-14: a reschedule books the new slot *before* it releases the old one, so moving
    an appointment by fifteen minutes overlaps its own predecessor for the length of the
    workflow. That overlap is the reschedule, not a clash. Kills: the predecessor
    exclusion removed (every short move would fail)."""
    first, second = await _two_providers(factory, _tomorrow_at(15), _tomorrow_at(15, 15))
    audit = RecordingAudit()
    old = await _book(factory, audit, first.patient_id, first.slot_id)
    new = await _pending(
        factory, audit, first.patient_id, second.slot_id, rescheduled_from_id=old
    )
    await _claim(factory, audit, new, second.slot_id)
    await _confirm(factory, audit, new)

    async with factory() as session:
        result = await service.complete_reschedule(
            session, audit, old_appointment_id=old, new_appointment_id=new,
            actor_user_id=None,
        )
        await session.commit()
    assert result.outcome == "rescheduled"
    assert (await _slot(factory, second.slot_id)).status is SlotStatus.BOOKED
    assert (await _slot(factory, first.slot_id)).status is SlotStatus.FREE


async def test_two_concurrent_overlapping_claims_for_one_patient_admit_one(factory):
    """The race the check alone cannot win: two claims for one patient at overlapping
    slots of different providers lock *different* slot rows, so nothing in the slot
    path serialises them, and each would read the other's claim as not yet committed.
    The advisory lock keyed on the patient makes the second wait for the first.

    Deterministic, like bkg-003: the first claim holds its transaction open, the second
    is shown to be parked, the first commits, and the second must then see it. Kills:
    the advisory lock removed (the second is not parked, and both claims succeed)."""
    first, second = await _two_providers(factory, _tomorrow_at(16), _tomorrow_at(16, 10))
    audit = RecordingAudit()
    a = await _pending(factory, audit, first.patient_id, first.slot_id)
    b = await _pending(factory, audit, first.patient_id, second.slot_id)

    winner = factory()
    try:
        await service.claim_slot(
            winner, audit, appointment_id=a, slot_id=first.slot_id, now=Clock().now(),
            actor_user_id=None,
        )
        loser = asyncio.create_task(_claim(factory, audit, b, second.slot_id))
        await asyncio.sleep(0.3)
        assert not loser.done(), "the second claim should wait on the patient's lock"
        await winner.commit()
    finally:
        await winner.close()

    with pytest.raises(Conflict, match="overlaps"):
        await loser
    assert (await _slot(factory, first.slot_id)).status is SlotStatus.HELD
    slot = await _slot(factory, second.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 0
