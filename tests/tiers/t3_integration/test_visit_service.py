"""Visit operations against real Postgres: the staff actions are strict (a repeat is a
Conflict, since only the endpoints call them and nothing retries), the no-show is
idempotent and settled under the appointment lock, and R-2 -- a completed or no-show
appointment's slot stays `booked`, untouched.

Tests say which mutation of `service.py` they exist to kill.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import Clock
from app.core.errors import Conflict, NotFound
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.scheduling import service
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
    Visit,
    VisitStatus,
)
from app.modules.scheduling.state import NoShowOutcome
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]

NO_SHOW_ACTOR = "system:visit-workflow"


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


async def _visits(factory, appointment_id) -> list[Visit]:
    async with factory() as session:
        rows = await session.scalars(
            select(Visit).where(Visit.appointment_id == appointment_id)
        )
        return list(rows)


async def _set_status(factory, appointment_id, status: AppointmentStatus) -> None:
    """Put an appointment into a state the visit path must refuse. Tests only."""
    async with factory() as session:
        appointment = await session.get(Appointment, appointment_id)
        appointment.status = status
        await session.commit()


async def _check_in(factory, audit, appointment_id) -> None:
    async with factory() as session:
        await service.check_in(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id="u-1",
        )
        await session.commit()


async def _seen(factory, audit, appointment_id) -> None:
    async with factory() as session:
        await service.mark_seen(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id="u-1",
        )
        await session.commit()


async def _complete(factory, audit, appointment_id) -> None:
    async with factory() as session:
        await service.complete_visit(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id="u-1",
        )
        await session.commit()


async def _no_show(factory, audit, appointment_id) -> NoShowOutcome:
    async with factory() as session:
        result = await service.mark_no_show(
            session, audit, appointment_id=appointment_id, actor_user_id=NO_SHOW_ACTOR
        )
        await session.commit()
        return result


def _actions(audit: RecordingAudit, entity: str) -> list[str]:
    return [e.action for e in audit.events if e.entity_type == entity]


# --- check_in ------------------------------------------------------------------------


async def test_check_in_inserts_one_visit_and_returns_it(factory):
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    async with factory() as session:
        returned = await service.check_in(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id="u-1",
        )
        await session.commit()

    (visit,) = await _visits(factory, appointment_id)
    assert returned.id == visit.id
    assert visit.status is VisitStatus.CHECKED_IN and visit.checked_in_at is not None
    assert _actions(audit, "visit") == ["checked_in"]
    assert audit.events[-1].actor_user_id == "u-1"


async def test_a_second_check_in_is_a_conflict_and_writes_nothing(factory):
    """Strict: a desk clerk checking a patient in twice is told so. Kills: the old
    "visit exists -> return" no-op restored in `check_in`."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)
    first = (await _visits(factory, appointment_id))[0]
    before = len(audit.events)

    with pytest.raises(Conflict):
        await _check_in(factory, audit, appointment_id)
    (visit,) = await _visits(factory, appointment_id)
    assert visit.checked_in_at == first.checked_in_at
    assert len(audit.events) == before


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.PENDING, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW],
)
async def test_check_in_needs_a_confirmed_appointment(factory, status):
    """Kills: the CONFIRMED requirement removed from `state.require_visit_action`."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _set_status(factory, appointment_id, status)
    with pytest.raises(Conflict):
        await _check_in(factory, audit, appointment_id)
    assert await _visits(factory, appointment_id) == []


async def test_check_in_for_a_missing_appointment_is_not_found(factory):
    async with factory() as session:
        with pytest.raises(NotFound):
            await service.check_in(
                session, RecordingAudit(), appointment_id=uuid.uuid4(),
                now=Clock().now(), actor_user_id=None,
            )


# --- mark_seen -----------------------------------------------------------------------


async def test_seen_before_check_in_is_a_conflict(factory):
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    with pytest.raises(Conflict):
        await _seen(factory, audit, appointment_id)


async def test_seen_twice_is_a_conflict_and_writes_once(factory):
    """Strict. Kills: the old "already in_progress -> return" no-op restored."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)
    await _seen(factory, audit, appointment_id)
    first = (await _visits(factory, appointment_id))[0]
    before = len(audit.events)
    with pytest.raises(Conflict):
        await _seen(factory, audit, appointment_id)

    visit = (await _visits(factory, appointment_id))[0]
    assert visit.status is VisitStatus.IN_PROGRESS
    assert visit.seen_at == first.seen_at and visit.seen_at is not None
    assert visit.seen_at >= visit.checked_in_at
    assert len(audit.events) == before
    assert _actions(audit, "visit") == ["checked_in", "seen"]


# --- complete_visit ------------------------------------------------------------------


async def test_complete_before_seen_is_a_conflict(factory):
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)
    with pytest.raises(Conflict):
        await _complete(factory, audit, appointment_id)
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED


async def test_complete_finishes_visit_and_appointment_and_leaves_the_slot_booked(factory):
    """R-2: a completed appointment's slot is in the past and stays `booked`.
    Kills: `complete_visit` writing the slot (status or version)."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    slot_before = await _slot(factory, b.slot_id)
    confirmed_at = (await _appointment(factory, appointment_id)).confirmed_at
    await _check_in(factory, audit, appointment_id)
    await _seen(factory, audit, appointment_id)
    await _complete(factory, audit, appointment_id)

    visit = (await _visits(factory, appointment_id))[0]
    assert visit.status is VisitStatus.COMPLETED and visit.completed_at is not None
    appointment = await _appointment(factory, appointment_id)
    assert appointment.status is AppointmentStatus.COMPLETED
    assert appointment.confirmed_at == confirmed_at
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BOOKED
    assert slot.version == slot_before.version


async def test_complete_twice_is_a_conflict_and_writes_nothing_the_second_time(factory):
    """Strict. Kills: the old "already completed -> return" no-op restored."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)
    await _seen(factory, audit, appointment_id)
    await _complete(factory, audit, appointment_id)
    before = len(audit.events)
    completed_at = (await _visits(factory, appointment_id))[0].completed_at
    with pytest.raises(Conflict):
        await _complete(factory, audit, appointment_id)

    assert len(audit.events) == before
    assert (await _visits(factory, appointment_id))[0].completed_at == completed_at
    assert _actions(audit, "visit")[-1] == "completed"
    assert _actions(audit, "appointment")[-1] == "completed"


# --- mark_no_show --------------------------------------------------------------------


async def test_no_show_with_no_visit_marks_it_and_leaves_the_slot_booked(factory):
    """Kills: `mark_no_show` writing the slot."""
    audit = RecordingAudit()
    b, appointment_id = await confirmed_appointment(factory, audit)
    slot_before = await _slot(factory, b.slot_id)

    assert await _no_show(factory, audit, appointment_id) is NoShowOutcome.NO_SHOW
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.NO_SHOW
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == slot_before.version
    assert audit.events[-1].action == "no_show"
    assert audit.events[-1].actor_user_id == NO_SHOW_ACTOR


async def test_no_show_twice_is_no_show_and_audits_once(factory):
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    assert await _no_show(factory, audit, appointment_id) is NoShowOutcome.NO_SHOW
    before = len(audit.events)
    assert await _no_show(factory, audit, appointment_id) is NoShowOutcome.NO_SHOW
    assert len(audit.events) == before


async def test_no_show_after_a_committed_check_in_is_visit_exists(factory):
    """The check-in reached the database, so the timer firing is not a no-show, whatever
    became of the check-in's signal to the workflow. Kills: the visit-exists guard removed
    from `mark_no_show`."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _check_in(factory, audit, appointment_id)
    before = len(audit.events)

    assert await _no_show(factory, audit, appointment_id) is NoShowOutcome.VISIT_EXISTS
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED
    assert len(audit.events) == before


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.RESCHEDULED, AppointmentStatus.COMPLETED],
)
async def test_no_show_on_an_appointment_that_left_confirmed_is_closed(factory, status):
    """Kills: the `confirmed` requirement removed from `mark_no_show`."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    await _set_status(factory, appointment_id, status)
    before = len(audit.events)

    assert await _no_show(factory, audit, appointment_id) is NoShowOutcome.CLOSED
    assert (await _appointment(factory, appointment_id)).status is status
    assert len(audit.events) == before


async def test_a_no_show_blocked_behind_an_uncommitted_check_in_finds_the_visit(factory):
    """The review's race, made deterministic: the timer fires while a check-in is
    mid-transaction. `mark_no_show` must wait on the appointment row lock the check-in
    holds, then see its committed visit -- not read "no visit yet" around the lock and
    record a patient standing at the desk as a no-show. Same technique as
    `test_a_claim_blocked_behind_a_winner_loses_on_the_update_predicate`. Kills:
    `mark_no_show` ignoring an existing visit, or reading without the lock."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)

    desk = factory()
    try:
        await service.check_in(
            desk, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id="u-1",
        )
        timer = asyncio.create_task(_no_show(factory, audit, appointment_id))
        await asyncio.sleep(0.3)
        assert not timer.done(), "mark_no_show should be waiting on the appointment lock"
        await desk.commit()
    finally:
        await desk.close()

    assert await timer is NoShowOutcome.VISIT_EXISTS
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED
    assert len(await _visits(factory, appointment_id)) == 1


# --- no_show_deadline -----------------------------------------------------------------


async def test_no_show_deadline_is_the_slots_end_plus_grace(factory):
    b, appointment_id = await confirmed_appointment(factory)
    slot = await _slot(factory, b.slot_id)
    async with factory() as session:
        deadline = await service.no_show_deadline(
            session, appointment_id=appointment_id, grace=timedelta(minutes=15)
        )
    assert deadline == slot.ends_at + timedelta(minutes=15)


async def test_no_show_deadline_for_a_missing_appointment_is_not_found(factory):
    async with factory() as session:
        with pytest.raises(NotFound):
            await service.no_show_deadline(
                session, appointment_id=uuid.uuid4(), grace=timedelta(minutes=15)
            )
