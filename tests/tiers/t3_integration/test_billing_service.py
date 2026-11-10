"""Billing against real Postgres and the payments-mock container (spec D-27).

The mock is shared by the run; every test resets its chaos switches before and after, and
uses appointment ids of its own, so the idempotency keys never collide.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events.models import OutboxEvent
from app.modules.billing import service
from app.modules.billing.gateway import GatewayUnavailable, PaymentGateway
from app.modules.billing.models import BillingRecord, BillingStatus
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

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
async def chaos(db_settings):
    async def set_chaos(**switches):
        async with httpx.AsyncClient(base_url=db_settings.payments_url) as client:
            await client.post("/_chaos", json=switches)

    await set_chaos()
    yield set_chaos
    await set_chaos()


async def _appointment(factory):
    b, appointment_id = await confirmed_appointment(factory, RecordingAudit())
    return b.patient_id, appointment_id


async def _authorize(factory, gateway, appointment_id, patient_id):
    async with factory() as session:
        record = await service.authorize(
            session, gateway, appointment_id=appointment_id, patient_id=patient_id,
            amount_minor=5000, currency="USD",
        )
        await session.commit()
        return record


async def test_authorize_capture_and_refund_each_happen_once(factory, db_settings, chaos):
    gateway = PaymentGateway(db_settings.payments_url)
    patient_id, appointment_id = await _appointment(factory)
    first = await _authorize(factory, gateway, appointment_id, patient_id)
    again = await _authorize(factory, gateway, appointment_id, patient_id)
    assert first.status is again.status is BillingStatus.AUTHORIZED
    assert first.authorization_ref == again.authorization_ref

    for _ in range(2):
        async with factory() as session:
            await service.capture(session, gateway, appointment_id=appointment_id)
            await session.commit()
    for _ in range(2):
        async with factory() as session:
            record = await service.release(session, gateway, appointment_id=appointment_id)
            await session.commit()
    assert record.status is BillingStatus.REFUNDED

    async with factory() as session:
        statuses = [
            row.payload["billing_status"] for row in (await session.scalars(
                select(OutboxEvent).where(OutboxEvent.topic == "billing.status_updated",
                                          OutboxEvent.key == str(appointment_id))
                .order_by(OutboxEvent.seq)
            )).all()
        ]
    # One event per real change; the repeats changed nothing and announced nothing.
    assert statuses == ["authorized", "captured", "refunded"]


async def test_a_timeout_after_the_gateway_acted_is_answered_by_the_retry(
    factory, db_settings, chaos
):
    """Referenced by tests/cases/bil-001-a-retried-charge-is-one-charge.yaml.

    The hard case: the gateway authorised, then the answer never arrived. The activity is
    retried; the same idempotency key gets the original authorisation, and there is one."""
    gateway = PaymentGateway(db_settings.payments_url, timeout_seconds=0.5)
    patient_id, appointment_id = await _appointment(factory)
    await chaos(timeout_after_commit_rate=1.0, hang_seconds=3)
    with pytest.raises(GatewayUnavailable):
        await _authorize(factory, gateway, appointment_id, patient_id)
    async with factory() as session:
        assert await session.scalar(
            select(BillingRecord).where(BillingRecord.appointment_id == appointment_id)
        ) is None, "nothing recorded: the commit never happened"

    await chaos()
    record = await _authorize(factory, gateway, appointment_id, patient_id)
    async with httpx.AsyncClient(base_url=db_settings.payments_url) as client:
        stored = (await client.get(f"/transactions/{record.authorization_ref}")).json()
    assert record.status is BillingStatus.AUTHORIZED
    assert stored["reference"] == str(appointment_id)


async def test_a_decline_is_recorded_not_rolled_back(factory, db_settings, chaos):
    gateway = PaymentGateway(db_settings.payments_url)
    patient_id, appointment_id = await _appointment(factory)
    await chaos(decline_customers=[str(patient_id)])
    record = await _authorize(factory, gateway, appointment_id, patient_id)
    assert record.status is BillingStatus.DECLINED and record.decline_reason
    async with factory() as session:
        stored = await session.scalar(
            select(BillingRecord).where(BillingRecord.appointment_id == appointment_id)
        )
    assert stored.status is BillingStatus.DECLINED
    async with factory() as session:  # releasing a decline has nothing to give back
        assert (await service.release(session, gateway, appointment_id=appointment_id)
                ).status is BillingStatus.DECLINED


async def test_a_gateway_outage_opens_the_circuit_and_fails_fast(factory, db_settings, chaos):
    from app.core.circuit import CircuitBreaker, CircuitOpen

    breaker = CircuitBreaker("test gateway", threshold=2, cooldown_seconds=60)
    gateway = PaymentGateway(db_settings.payments_url, breaker=breaker)
    patient_id, appointment_id = await _appointment(factory)
    await chaos(failure_rate=1.0)
    for _ in range(2):
        with pytest.raises(GatewayUnavailable):
            await _authorize(factory, gateway, appointment_id, patient_id)
    with pytest.raises(CircuitOpen):
        await _authorize(factory, gateway, appointment_id, patient_id)


async def test_the_database_refuses_an_unknown_billing_status(factory):
    """`ck_billing_records_billingstatus` is excluded from `alembic check`
    (migrations/env.py); this is the proof the database enforces it."""
    patient_id, appointment_id = await _appointment(factory)
    async with factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(text(
                "INSERT INTO billing_records (id, appointment_id, patient_id, status, amount_minor)"
                " VALUES (:id, :a, :p, 'teleported', 1)"
            ), {"id": uuid.uuid4(), "a": appointment_id, "p": patient_id})
