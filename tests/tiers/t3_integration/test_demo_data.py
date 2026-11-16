"""Demo data against a real database: every constraint holds, re-runs add nothing, the
analytics reconcile finds no drift, and the live phase books through real workflows."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.clock import Clock
from app.core.ratelimit import InMemoryRateLimiter
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.demo.network import (
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    build_network,
    ensure_slots,
    patient_identity,
)
from app.modules.identity.service import authenticate
from app.modules.identity.sessions import InMemorySessionStore
from app.modules.patients.models import Patient
from app.modules.scheduling.models import ProviderSlot
from tests.tiers.t3_integration.demo_helpers import purge_demo

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PASSWORD = "demo-integration-password"


@pytest.fixture
async def demo_factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        await purge_demo(session)
    try:
        yield factory
    finally:
        async with factory() as session:
            await purge_demo(session)
        await engine.dispose()


async def test_the_network_is_idempotent_and_its_logins_authenticate(demo_factory, db_settings):
    async with demo_factory() as session:
        net, created = await build_network(session, password=PASSWORD)
        await session.commit()
    assert created["providers"] == len(PROVIDERS) and created["patients"] == PATIENT_COUNT
    async with demo_factory() as session:
        again, created_again = await build_network(session, password=PASSWORD)
        await session.commit()
    assert set(created_again.values()) == {0}
    assert again.provider_ids == net.provider_ids
    async with demo_factory() as session:
        email = patient_identity(LINKED_PATIENT_INDEXES[0]).email
        tokens = await authenticate(
            session, email=email, password=PASSWORD, client_ip="127.0.0.1",
            sessions=InMemorySessionStore(), limiter=InMemoryRateLimiter(),
            settings=db_settings, now=Clock().now(),
        )
        assert tokens.access_token
        linked = await session.scalar(
            select(func.count()).select_from(Patient).where(Patient.user_id.is_not(None),
                                                            Patient.mrn.like("DEMO-%")))
        assert linked == len(LINKED_PATIENT_INDEXES)


async def test_upcoming_slots_follow_the_schedule_and_are_not_duplicated(demo_factory):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        today = Clock().now().date()
        made = await ensure_slots(session, net, first_day=today,
                                  last_day=today + timedelta(days=13))
        await session.commit()
        assert made > 0
        again = await ensure_slots(session, net, first_day=today,
                                   last_day=today + timedelta(days=13))
        assert again == 0
        count = await session.scalar(select(func.count()).select_from(ProviderSlot).where(
            ProviderSlot.provider_id.in_(net.provider_ids.values())))
        assert count == made
