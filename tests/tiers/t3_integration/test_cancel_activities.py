"""The slice-4 activities, called directly against real Postgres: each is one service call
in one transaction, answers JSON-friendly values, and is safe to run twice."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio.exceptions import ApplicationError

from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.scheduling import service
from app.modules.scheduling.activities import SchedulingActivities
from app.modules.scheduling.contracts import BookingRequest, RescheduleRequest
from app.modules.scheduling.models import (
    Appointment,
    ProviderSlot,
    SlotStatus,
    WaitlistEntry,
    WaitlistStatus,
)
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import (
    RecordingAudit,
    add_slot,
    confirmed_appointment,
    seed_bookable,
)

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


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def activities(factory, audit) -> SchedulingActivities:
    return SchedulingActivities(factory, audit, Clock(), timedelta(minutes=15))


async def test_create_pending_carries_the_reschedule_link_into_the_row(
    factory, activities
):
    """Kills: `rescheduled_from_id` not passed through to `service.create_pending`."""
    audit = RecordingAudit()
    b, old_id = await confirmed_appointment(factory, audit)
    async with factory() as session:
        new_slot = await add_slot(session, b)
    new_id = uuid.uuid4()

    request = BookingRequest(
        str(new_id), str(b.patient_id), str(new_slot), None, None,
        rescheduled_from_id=str(old_id),
    )
    await activities.create_pending(request)
    await activities.create_pending(request)  # a retry is harmless

    async with factory() as session:
        row = await session.get(Appointment, new_id)
    assert row.rescheduled_from_id == old_id


async def test_reschedule_complete_answers_json_and_repeats_identically(
    factory, activities, audit
):
    b, old_id = await confirmed_appointment(factory, audit)
    request = RescheduleRequest(str(old_id), str(uuid.uuid4()), str(b.patient_id),
                                str(b.slot_id), "u-1")

    first = await activities.reschedule_complete(request)
    second = await activities.reschedule_complete(request)

    assert first == {"outcome": "rescheduled", "released_slot_id": str(b.slot_id)}
    assert second == first
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.FREE


async def test_reschedule_complete_of_a_cancelled_appointment_is_superseded(
    factory, activities, audit
):
    b, old_id = await confirmed_appointment(factory, audit)
    async with factory() as session:
        await service.cancel_appointment(
            session, audit, appointment_id=old_id, reason="x", now=Clock().now(),
            actor_user_id=None,
        )
        await session.commit()
    result = await activities.reschedule_complete(
        RescheduleRequest(str(old_id), str(uuid.uuid4()), str(b.patient_id),
                          str(b.slot_id), None)
    )
    assert result == {"outcome": "superseded", "released_slot_id": None}


async def test_waitlist_candidates_are_string_pairs_and_fulfil_removes_them(
    factory, activities, audit
):
    async with factory() as session:
        b = await seed_bookable(session)
        slot = await session.get(ProviderSlot, b.slot_id)
    async with factory() as session:
        entry = await service.add_waitlist_entry(
            session, audit, patient_id=b.patient_id, provider_id=None, department_id=None,
            window_start=slot.starts_at - timedelta(hours=1),
            window_end=slot.ends_at, actor_user_id=None,
        )
        await session.commit()
        entry_id = entry.id

    assert await activities.waitlist_candidates(str(b.slot_id)) == [
        [str(entry_id), str(b.patient_id)]
    ]
    await activities.waitlist_fulfil(str(entry_id))
    await activities.waitlist_fulfil(str(entry_id))  # a retry is harmless

    assert await activities.waitlist_candidates(str(b.slot_id)) == []
    async with factory() as session:
        assert (await session.get(WaitlistEntry, entry_id)).status is WaitlistStatus.FULFILLED


async def test_a_domain_error_becomes_a_non_retryable_application_error(activities):
    with pytest.raises(ApplicationError) as caught:
        await activities.waitlist_fulfil(str(uuid.uuid4()))
    assert caught.value.non_retryable and caught.value.type == "NotFound"
