"""The beat sweeps against real Postgres and Temporal: the stale-pending reconciler (PRD
R-9) and the outbox purge."""

from __future__ import annotations

import uuid


import pytest
from sqlalchemy import text

from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events.maintenance import purge_published
from app.events.outbox import emit
from app.modules.scheduling import service
from app.modules.scheduling.models import Appointment, AppointmentStatus, ProviderSlot, SlotStatus
from app.modules.scheduling.tasks import reconcile
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


async def _orphan(factory) -> tuple[uuid.UUID, uuid.UUID]:
    """A pending appointment holding its slot, with no booking workflow -- what a
    terminated workflow leaves behind -- and old enough to count as stale."""
    async with factory() as session:
        b = await seed_bookable(session)
    appointment_id = uuid.uuid4()
    audit = RecordingAudit()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=b.slot_id, reason=None, actor_user_id=None,
        )
        await session.commit()
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
            now=Clock().now(), actor_user_id=None,
        )
        await session.commit()
    async with factory() as session:  # created_at is not trigger-owned; backdate it
        await session.execute(
            text("UPDATE appointments SET created_at = created_at - interval '1 hour' "
                 "WHERE id = :id"), {"id": appointment_id},
        )
        await session.commit()
    return appointment_id, b.slot_id


async def test_an_orphaned_pending_booking_is_compensated(factory, db_settings):
    """Referenced by tests/cases/bkg-011-an-orphaned-pending-booking-is-reconciled.yaml.

    PRD R-9: a booking workflow terminated mid-flight runs no compensation. The sweep finds
    the stale pending row with no running workflow and does what fail_booking would have."""
    appointment_id, slot_id = await _orphan(factory)
    released: list[uuid.UUID] = []

    async def release_billing(session, appointment):
        released.append(appointment)

    fixed = await reconcile(db_settings, release_billing=release_billing)
    assert str(appointment_id) in fixed
    async with factory() as session:
        assert (await session.get(Appointment, appointment_id)).status is AppointmentStatus.FAILED
        assert (await session.get(ProviderSlot, slot_id)).status is SlotStatus.FREE
    assert released == [appointment_id]
    # A second sweep finds nothing: fail_booking acts only on pending rows.
    assert str(appointment_id) not in await reconcile(db_settings)


async def test_a_young_pending_booking_is_left_alone(factory, db_settings):
    async with factory() as session:
        b = await seed_bookable(session)
    appointment_id = uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, RecordingAudit(), appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=b.slot_id, reason=None, actor_user_id=None,
        )
        await session.commit()
    assert str(appointment_id) not in await reconcile(db_settings)
    async with factory() as session:
        assert (await session.get(Appointment, appointment_id)).status is AppointmentStatus.PENDING


async def test_the_purge_deletes_old_published_rows_and_never_the_backlog(factory, db_settings):
    async with factory() as session:
        old = emit(session, "clinics.state", key="k-old", payload={
            "clinic_id": str(uuid.uuid4()), "name": "n", "timezone": "UTC", "address": None,
            "is_active": True})
        backlog = emit(session, "clinics.state", key="k-backlog", payload={
            "clinic_id": str(uuid.uuid4()), "name": "n", "timezone": "UTC", "address": None,
            "is_active": True})
        await session.flush()
        old_seq, backlog_seq = old.seq, backlog.seq
        await session.execute(
            text("UPDATE outbox_events SET published_at = clock_timestamp() - interval '8 days',"
                 " created_at = created_at - interval '8 days' WHERE seq = :seq"),
            {"seq": old_seq},
        )
        await session.execute(
            text("UPDATE outbox_events SET created_at = created_at - interval '30 days' "
                 "WHERE seq = :seq"), {"seq": backlog_seq},
        )
        await session.commit()
    assert await purge_published(db_settings) >= 1
    async with factory() as session:
        remaining = set((await session.execute(
            text("SELECT seq FROM outbox_events WHERE seq IN (:a, :b)"),
            {"a": old_seq, "b": backlog_seq},
        )).scalars())
    assert remaining == {backlog_seq}, "an unpublished row is never purged, however old"


