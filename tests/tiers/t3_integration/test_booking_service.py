"""Booking operations against real Postgres: idempotency, the conditional claim, the
paired write, and the rules the schema cannot enforce (R-3, R-4).

Several tests exist to kill a specific mutation of `service.py` -- a predicate removed
from a WHERE, a guard deleted -- that the happy paths alone let survive. Each says which.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import Clock
from app.core.errors import Conflict, NotFound, Unprocessable
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.scheduling import service
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
)
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import add_slot, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


class _NullAudit:
    def __init__(self) -> None:
        self.events = []

    async def record(self, event) -> None:
        self.events.append(event)


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


async def _set_slot_status(factory, slot_id, status: SlotStatus) -> None:
    """Put a slot into a state no service call would leave it in. Tests only: the
    slot-write guard scans `app/`."""
    async with factory() as session:
        slot = await session.get(ProviderSlot, slot_id)
        slot.status = status
        await session.commit()


async def _pending(factory, b, audit, appointment_id=None, *, slot_id=None) -> uuid.UUID:
    appointment_id = appointment_id or uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=slot_id or b.slot_id, reason=None, actor_user_id=None,
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


async def _fail(factory, audit, appointment_id) -> None:
    async with factory() as session:
        await service.fail_booking(
            session, audit, appointment_id=appointment_id, actor_user_id=None
        )
        await session.commit()


# --- create_pending ------------------------------------------------------------------


async def test_create_pending_is_idempotent(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _pending(factory, b, audit, appointment_id)  # a retried activity

    async with factory() as session:
        rows = (await session.scalars(
            select(Appointment).where(Appointment.id == appointment_id)
        )).all()
    assert len(rows) == 1 and rows[0].status is AppointmentStatus.PENDING
    assert rows[0].provider_id == b.provider_id and rows[0].clinic_id == b.clinic_id
    assert len(audit.events) == 1, "a retry must not write a second audit entry"


async def test_create_pending_rejects_an_id_reused_for_another_patient(factory):
    """ON CONFLICT DO NOTHING alone would report success for someone else's row."""
    async with factory() as session:
        b = await seed_bookable(session)
        other = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)

    with pytest.raises(Conflict, match="reused"):
        await _pending(factory, other, audit, appointment_id, slot_id=b.slot_id)
    assert (await _appointment(factory, appointment_id)).patient_id == b.patient_id


async def test_create_pending_for_an_unknown_patient_is_not_found(factory):
    """A raw IntegrityError would be retried by Temporal; NotFound is permanent."""
    async with factory() as session:
        b = await seed_bookable(session)
    async with factory() as session:
        with pytest.raises(NotFound, match="patient"):
            await service.create_pending(
                session, _NullAudit(), appointment_id=uuid.uuid4(), patient_id=uuid.uuid4(),
                slot_id=b.slot_id, reason=None, actor_user_id=None,
            )


async def test_a_past_slot_is_rejected(factory):
    """R-3: CHECK (starts_at > now()) is impossible in Postgres, so the service owns it."""
    async with factory() as session:
        b = await seed_bookable(session, starts_in=timedelta(hours=-1))
    async with factory() as session:
        with pytest.raises(Unprocessable):
            await service.ensure_slot_bookable(session, slot_id=b.slot_id, now=Clock().now())


# --- claim_slot ----------------------------------------------------------------------


async def test_claim_increments_version_and_is_idempotent(factory):
    """R-4: version must move on every claim, or the column is decoration.
    Kills: `version=ProviderSlot.version + 1` removed from the claim."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    for _ in range(2):  # the second call is a retried activity
        await _claim(factory, audit, appointment_id, b.slot_id)

    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.HELD and slot.version == 1
    assert (await _appointment(factory, appointment_id)).slot_id == b.slot_id


async def test_a_claim_blocked_behind_a_winner_loses_on_the_update_predicate(factory):
    """The conditional UPDATE is the whole concurrency story (PRD D-2). Deterministic:
    the second claim is parked on the first's row lock until the first commits, then
    Postgres re-evaluates `status = 'free'` against the committed row and matches nothing.
    The message pins *which* gate refused it -- the partial index would say "held by
    another appointment". Kills: the FREE predicate removed (the loser would re-claim the
    slot, bump version to 2, and only the index would stop it)."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    first, second = await _pending(factory, b, audit), await _pending(factory, b, audit)

    winner = factory()
    try:
        await service.claim_slot(
            winner, audit, appointment_id=first, slot_id=b.slot_id, now=Clock().now(),
            actor_user_id=None,
        )
        loser = asyncio.create_task(_claim(factory, audit, second, b.slot_id))
        await asyncio.sleep(0.2)
        assert not loser.done(), "the second claim should be waiting on the row lock"
        await winner.commit()
    finally:
        await winner.close()

    with pytest.raises(Conflict, match="no longer free"):
        await loser
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.HELD and slot.version == 1
    assert (await _appointment(factory, first)).slot_id == b.slot_id
    assert (await _appointment(factory, second)).slot_id is None


@pytest.mark.parametrize("status", [SlotStatus.BLOCKED, SlotStatus.BOOKED])
async def test_only_a_free_slot_can_be_claimed(factory, status):
    """No live appointment holds the slot, so the partial index has nothing to say: only
    `status = 'free'` in the claim's WHERE can refuse it. Kills: the FREE predicate
    removed."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _set_slot_status(factory, b.slot_id, status)

    with pytest.raises(Conflict, match="no longer free"):
        await _claim(factory, audit, appointment_id, b.slot_id)
    slot = await _slot(factory, b.slot_id)
    assert slot.status is status and slot.version == 0


async def test_a_slot_that_started_before_the_claim_is_unprocessable(factory):
    """R-3 at time of use: `ensure_slot_bookable` ran in an earlier transaction, and a
    retry or a backlog can reach the claim after the slot started. Kills: the
    `starts_at > now` predicate removed."""
    async with factory() as session:
        b = await seed_bookable(session, starts_in=timedelta(hours=-1))
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)

    with pytest.raises(Unprocessable):
        await _claim(factory, audit, appointment_id, b.slot_id)
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 0


async def test_claiming_a_missing_slot_is_not_found(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    with pytest.raises(NotFound, match="slot"):
        await _claim(factory, audit, appointment_id, uuid.uuid4())


async def test_a_failed_appointment_cannot_claim(factory):
    """Kills: the PENDING check removed (the failed row would claim a free slot)."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _fail(factory, audit, appointment_id)

    with pytest.raises(Conflict, match="failed"):
        await _claim(factory, audit, appointment_id, b.slot_id)
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 0


async def test_re_claiming_its_own_slot_after_failing_is_a_conflict(factory):
    """A failed appointment keeps `slot_id`; matching it must not read as a retry."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    await _fail(factory, audit, appointment_id)

    with pytest.raises(Conflict, match="failed"):
        await _claim(factory, audit, appointment_id, b.slot_id)
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.FREE


async def test_a_confirmed_appointment_cannot_claim_another_slot(factory):
    async with factory() as session:
        b = await seed_bookable(session)
        second_slot = await add_slot(session, b)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    await _confirm(factory, audit, appointment_id)

    with pytest.raises(Conflict, match="confirmed"):
        await _claim(factory, audit, appointment_id, second_slot)
    assert (await _slot(factory, second_slot)).status is SlotStatus.FREE


async def test_a_second_claim_of_a_different_slot_is_a_conflict(factory):
    """Overwriting `slot_id` would orphan the first slot `held` forever."""
    async with factory() as session:
        b = await seed_bookable(session)
        second_slot = await add_slot(session, b)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)

    with pytest.raises(Conflict, match="already holds"):
        await _claim(factory, audit, appointment_id, second_slot)
    second = await _slot(factory, second_slot)
    assert second.status is SlotStatus.FREE and second.version == 0
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.HELD
    assert (await _appointment(factory, appointment_id)).slot_id == b.slot_id


async def test_claiming_another_providers_slot_is_a_conflict(factory):
    """The composite coherence FK (migration 0002) refuses it; that must reach the
    caller as a permanent Conflict, not a retryable IntegrityError."""
    async with factory() as session:
        b = await seed_bookable(session)
        other = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)

    with pytest.raises(Conflict, match="another provider or clinic"):
        await _claim(factory, audit, appointment_id, other.slot_id)
    slot = await _slot(factory, other.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 0


# --- confirm_booking -----------------------------------------------------------------


async def test_confirm_books_the_slot_and_is_idempotent(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    for _ in range(2):
        await _confirm(factory, audit, appointment_id)

    appointment = await _appointment(factory, appointment_id)
    assert appointment.status is AppointmentStatus.CONFIRMED
    assert appointment.confirmed_at is not None
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == 2


async def test_confirm_without_a_claim_is_a_conflict(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    with pytest.raises(Conflict, match="holds no slot"):
        await _confirm(factory, audit, appointment_id)
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.PENDING


async def test_confirm_after_failing_is_a_conflict(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    await _fail(factory, audit, appointment_id)

    with pytest.raises(Conflict):
        await _confirm(factory, audit, appointment_id)
    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.FAILED
    assert (await _slot(factory, b.slot_id)).status is SlotStatus.FREE


# --- fail_booking --------------------------------------------------------------------


async def test_fail_booking_releases_its_held_slot(factory):
    """Compensation is a paired write (R-2): appointment failed and slot free, together."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    for _ in range(2):  # idempotent
        await _fail(factory, audit, appointment_id)

    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.FAILED
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 2


async def test_fail_booking_after_confirm_is_a_no_op(factory):
    """Something else owns a confirmed appointment; compensation must not touch it."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    await _confirm(factory, audit, appointment_id)
    await _fail(factory, audit, appointment_id)

    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.CONFIRMED
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == 2


async def test_fail_booking_frees_only_a_slot_that_is_still_held(factory):
    """The appointment fails, but a slot moved out of `held` behind its back (here,
    blocked) stays as it is. Kills: `status == HELD` removed from the release."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    await _claim(factory, audit, appointment_id, b.slot_id)
    await _set_slot_status(factory, b.slot_id, SlotStatus.BLOCKED)
    await _fail(factory, audit, appointment_id)

    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.FAILED
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BLOCKED and slot.version == 1


async def test_fail_booking_with_no_row_is_a_no_op(factory):
    """create_pending may itself have failed; compensation must not."""
    async with factory() as session:
        await service.fail_booking(
            session, _NullAudit(), appointment_id=uuid.uuid4(), actor_user_id=None
        )
