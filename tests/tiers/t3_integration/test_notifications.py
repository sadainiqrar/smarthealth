"""Notifications against real Postgres, Redis, RabbitMQ and Mailpit.

What these prove, in the order a message lives:

1. an event queues one message, however often it is delivered (the unique key);
2. the send task delivers it -- a real email arrives in Mailpit -- and a second delivery of
   the same task sends nothing (the claim);
3. a channel failure is retried with backoff, then fails for good;
4. a patient over the hourly limit waits instead of being flooded;
5. end to end through Kafka and RabbitMQ: a booking's event becomes an email, via the
   relay, the consumer, an enqueue after commit and a real Celery worker.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events.consumer import ConsumerContext, Event
from app.modules.notifications import tasks
from app.modules.notifications.consumers import on_patient_event
from app.modules.notifications.models import Notification, NotificationStatus
from app.modules.patients.models import Patient
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]

MAILPIT = "http://localhost:18025"


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


async def _patient_with_email(factory) -> tuple[uuid.UUID, uuid.UUID, str]:
    b, appointment_id = await confirmed_appointment(factory, RecordingAudit())
    email = f"patient-{uuid.uuid4().hex[:10]}@example.test"
    async with factory() as session:
        await session.execute(update(Patient).where(Patient.id == b.patient_id).values(email=email))
        await session.commit()
    return b.patient_id, appointment_id, email


def _booked_event(patient_id, appointment_id) -> Event:
    return Event(
        event_id=str(uuid.uuid4()), event_type="appointments.booked",
        subject_id=str(appointment_id),
        occurred_at=datetime.now(UTC), topic="appointments.booked", partition=0, offset=0,
        payload={"appointment_id": str(appointment_id), "patient_id": str(patient_id),
                 "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
    )


async def _queue_one(factory, settings, patient_id, appointment_id, enqueued: list[str]):
    """Run the consumer's handler as the runtime would, with a recording enqueue."""
    from app.modules.notifications import consumers

    original = consumers.enqueue_send
    consumers.enqueue_send = enqueued.append
    try:
        event = _booked_event(patient_id, appointment_id)
        for _ in range(2):  # the redelivery
            async with factory() as session:
                await on_patient_event(session, event, ConsumerContext(settings=settings))
                await session.commit()
    finally:
        consumers.enqueue_send = original


async def _mail_to(address: str, timeout: float = 20) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(base_url=MAILPIT) as client:
        while True:
            found = (await client.get("/api/v1/search", params={"query": f"to:{address}"})).json()
            if found["messages"] or asyncio.get_running_loop().time() > deadline:
                return found["messages"]
            await asyncio.sleep(0.5)


async def test_an_event_delivered_twice_queues_one_message(factory, db_settings):
    patient_id, appointment_id, email = await _patient_with_email(factory)
    enqueued: list[str] = []
    await _queue_one(factory, db_settings, patient_id, appointment_id, enqueued)
    assert len(enqueued) == 1, "one enqueue, and only after the commit"
    async with factory() as session:
        rows = (await session.scalars(
            select(Notification).where(Notification.appointment_id == appointment_id)
        )).all()
    assert len(rows) == 1
    assert rows[0].recipient == email and rows[0].kind == "booking_confirmed"
    assert rows[0].status is NotificationStatus.QUEUED


async def test_the_send_task_delivers_once(factory, db_settings):
    """Referenced by tests/cases/ntf-001-a-message-is-delivered-once.yaml."""
    patient_id, appointment_id, email = await _patient_with_email(factory)
    enqueued: list[str] = []
    await _queue_one(factory, db_settings, patient_id, appointment_id, enqueued)
    notification_id = uuid.UUID(enqueued[0])

    first = await tasks.send(db_settings, notification_id, retries=0)
    second = await tasks.send(db_settings, notification_id, retries=0)  # a redelivered task
    assert (first.status, second.status) == ("sent", "duplicate")
    messages = await _mail_to(email)
    assert len(messages) == 1
    assert messages[0]["Subject"] == "Your appointment is confirmed"
    async with factory() as session:
        row = await session.get(Notification, notification_id)
    assert row.status is NotificationStatus.SENT and row.provider_message_id


async def test_a_channel_failure_is_retried_then_fails_for_good(factory, db_settings):
    patient_id, appointment_id, _ = await _patient_with_email(factory)
    enqueued: list[str] = []
    await _queue_one(factory, db_settings, patient_id, appointment_id, enqueued)
    notification_id = uuid.UUID(enqueued[0])
    broken = db_settings.model_copy(update={"smtp_port": 1})  # nothing listens there

    retry = await tasks.send(broken, notification_id, retries=0)
    assert retry.status == "retry" and retry.retry_in >= 5
    async with factory() as session:
        row = await session.get(Notification, notification_id)
    assert row.status is NotificationStatus.QUEUED
    final = await tasks.send(broken, notification_id, retries=tasks.MAX_RETRIES)
    assert final.status == "failed"
    async with factory() as session:
        row = await session.get(Notification, notification_id)
    assert row.status is NotificationStatus.FAILED and "smtp" in row.last_error


async def test_a_patient_over_the_hourly_limit_waits(factory, db_settings):
    patient_id, appointment_id, _ = await _patient_with_email(factory)
    enqueued: list[str] = []
    await _queue_one(factory, db_settings, patient_id, appointment_id, enqueued)
    limited = db_settings.model_copy(update={"notification_hourly_limit": 1})
    from app.core.ratelimit import RedisRateLimiter
    from app.db.redis import create_redis_client

    redis = create_redis_client(limited)
    try:
        await RedisRateLimiter(redis, limited).hit(
            f"notify:{patient_id}", limit=1, window=timedelta(hours=1)
        )
    finally:
        await redis.aclose()
    outcome = await tasks.send(limited, uuid.UUID(enqueued[0]), retries=0)
    assert outcome.status == "throttled" and outcome.retry_in > 0


async def test_the_sweep_requeues_a_message_whose_enqueue_was_lost(factory, db_settings):
    patient_id, appointment_id, _ = await _patient_with_email(factory)
    enqueued: list[str] = []
    await _queue_one(factory, db_settings, patient_id, appointment_id, enqueued)
    notification_id = uuid.UUID(enqueued[0])
    from app.modules.notifications import service

    # As if the process died before enqueueing, and five minutes have passed. (The row's
    # updated_at cannot be backdated: the BEFORE UPDATE trigger owns it.)
    later = Clock().now() + timedelta(minutes=5)
    async with factory() as session:
        stuck = await service.stuck(session, later, older_than=tasks.STUCK_AFTER)
    assert notification_id in stuck


async def test_a_booking_becomes_an_email_through_kafka_and_celery(factory, db_settings):
    """Referenced by tests/cases/ntf-003-a-booking-email-end-to-end.yaml.

    confirm_booking writes appointments.booked -> the relay publishes it -> the
    notifications consumer queues a message in its transaction and enqueues the send after
    the commit -> a real Celery worker on RabbitMQ sends it -> Mailpit has it."""
    from celery.contrib.testing.worker import start_worker

    from app.celery_app import celery, configure
    from app.core.temporal import connect_client

    configure(db_settings)
    for module in ("app.modules.notifications.tasks",):
        __import__(module)
    client = await connect_client(db_settings, lazy=False)
    b = None
    with start_worker(celery, perform_ping_check=False, shutdown_timeout=20,
                      queues=[celery.conf.task_default_queue]):
        async with scheduling_helpers.event_pipeline(db_settings, factory, client):
            async with factory() as session:
                b = await scheduling_helpers.seed_bookable(session)
                email = f"e2e-{uuid.uuid4().hex[:10]}@example.test"
                await session.execute(
                    update(Patient).where(Patient.id == b.patient_id).values(email=email)
                )
                await session.commit()
            from app.modules.scheduling import service

            audit = RecordingAudit()
            appointment_id = uuid.uuid4()
            for step in ("create_pending", "claim_slot", "confirm_booking"):
                async with factory() as session:
                    if step == "create_pending":
                        await service.create_pending(
                            session, audit, appointment_id=appointment_id,
                            patient_id=b.patient_id, slot_id=b.slot_id, reason=None,
                            actor_user_id=None,
                        )
                    elif step == "claim_slot":
                        await service.claim_slot(
                            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
                            now=Clock().now(), actor_user_id=None,
                        )
                    else:
                        await service.confirm_booking(
                            session, audit, appointment_id=appointment_id, now=Clock().now(),
                            actor_user_id=None,
                        )
                    await session.commit()
            messages = await _mail_to(email, timeout=60)
    assert [m["Subject"] for m in messages] == ["Your appointment is confirmed"]
