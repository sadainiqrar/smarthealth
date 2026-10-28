"""Booking operations against real Postgres: idempotency, the conditional claim, the
paired write, and the rules the schema cannot enforce (R-3, R-4)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import Clock
from app.core.errors import Unprocessable
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
from tests.tiers.t3_integration.scheduling_helpers import seed_bookable

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


async def _pending(factory, b, audit, appointment_id=None) -> uuid.UUID:
    appointment_id = appointment_id or uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=b.slot_id, reason=None, actor_user_id=None,
        )
        await session.commit()
    return appointment_id


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


async def test_a_past_slot_is_rejected(factory):
    """R-3: CHECK (starts_at > now()) is impossible in Postgres, so the service owns it."""
    async with factory() as session:
        b = await seed_bookable(session, starts_in=timedelta(hours=-1))
    async with factory() as session:
        with pytest.raises(Unprocessable):
            await service.ensure_slot_bookable(session, slot_id=b.slot_id, now=Clock().now())


async def test_claim_increments_version_and_is_idempotent(factory):
    """R-4: version must move on every claim, or the column is decoration."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    for _ in range(2):  # the second call is a retried activity
        async with factory() as session:
            await service.claim_slot(
                session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
                actor_user_id=None,
            )
            await session.commit()

    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.HELD and slot.version == 1
    assert (await _appointment(factory, appointment_id)).slot_id == b.slot_id


async def test_two_concurrent_claims_have_exactly_one_winner(factory):
    """The conditional UPDATE is the whole concurrency story (PRD D-2): zero rows
    affected is the conflict, with no window between checking and acting."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    first, second = await _pending(factory, b, audit), await _pending(factory, b, audit)

    async def claim(appointment_id):
        async with factory() as session:
            await service.claim_slot(
                session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
                actor_user_id=None,
            )
            await session.commit()

    results = await asyncio.gather(claim(first), claim(second), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "NoneType"]
    assert (await _slot(factory, b.slot_id)).version == 1


async def test_confirm_books_the_slot_and_is_idempotent(factory):
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
            actor_user_id=None,
        )
        await session.commit()
    for _ in range(2):
        async with factory() as session:
            await service.confirm_booking(
                session, audit, appointment_id=appointment_id, now=Clock().now(),
                actor_user_id=None,
            )
            await session.commit()

    appointment = await _appointment(factory, appointment_id)
    assert appointment.status is AppointmentStatus.CONFIRMED
    assert appointment.confirmed_at is not None
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == 2


async def test_fail_booking_releases_only_its_own_held_slot(factory):
    """Compensation is a paired write (R-2): appointment failed and slot free, together."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    appointment_id = await _pending(factory, b, audit)
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
            actor_user_id=None,
        )
        await session.commit()
    for _ in range(2):  # idempotent
        async with factory() as session:
            await service.fail_booking(
                session, audit, appointment_id=appointment_id, actor_user_id=None
            )
            await session.commit()

    assert (await _appointment(factory, appointment_id)).status is AppointmentStatus.FAILED
    slot = await _slot(factory, b.slot_id)
    assert slot.status is SlotStatus.FREE and slot.version == 2


async def test_fail_booking_never_frees_a_slot_another_booking_holds(factory):
    """The loser of a race compensates; the winner's slot must stay held."""
    async with factory() as session:
        b = await seed_bookable(session)
    audit = _NullAudit()
    winner, loser = await _pending(factory, b, audit), await _pending(factory, b, audit)
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=winner, slot_id=b.slot_id, actor_user_id=None
        )
        await session.commit()
    async with factory() as session:
        await service.fail_booking(session, audit, appointment_id=loser, actor_user_id=None)
        await session.commit()

    assert (await _slot(factory, b.slot_id)).status is SlotStatus.HELD


async def test_fail_booking_with_no_row_is_a_no_op(factory):
    """create_pending may itself have failed; compensation must not."""
    async with factory() as session:
        await service.fail_booking(
            session, _NullAudit(), appointment_id=uuid.uuid4(), actor_user_id=None
        )
