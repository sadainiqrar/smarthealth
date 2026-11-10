"""The consumer runtime against real Kafka and Postgres: dedupe, retry, dead-letter.

The handlers here are test doubles whose side effect is a row in `processed_events`
under a second consumer name -- a real write in the handler's transaction, so "rolled
back with the dedupe mark" and "applied exactly once" are observable.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from confluent_kafka import TopicPartition
from sqlalchemy import func, select

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events import kafka
from app.events.consumer import ConsumerRunner, ConsumerSpec, Outcome
from app.events.models import OutboxEvent, ProcessedEvent
from app.events.relay import Relay
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture(scope="session")
def backbone(db_settings):
    kafka.ensure_topics(db_settings)
    kafka.register_schemas(db_settings)
    return db_settings


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


async def _booked_message(factory, settings):
    """Publish one fresh appointments.booked event and fetch its Kafka message."""
    _, appointment_id = await confirmed_appointment(factory, RecordingAudit())
    async with factory() as session:
        row = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.key == str(appointment_id),
                                      OutboxEvent.topic == "appointments.booked")
        )
    relay = Relay(settings, factory)
    while (await relay.run_once()).published:
        pass
    return _fetch(settings, "appointments.booked", str(row.event_id))


def _fetch(settings, name: str, event_id: str, timeout: float = 30):
    consumer = kafka.consumer(settings, f"fetch-{uuid.uuid4().hex[:8]}")
    topic_name = kafka.broker_topic(settings, name)
    parts = consumer.list_topics(topic_name, timeout=10).topics[topic_name].partitions
    consumer.assign([TopicPartition(topic_name, p, 0) for p in parts])
    decode = kafka.deserializer(kafka.schema_registry(settings))
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is None or message.error():
                continue
            if name.endswith(".dlt"):
                if json.loads(message.value())["event"]["event_id"] == event_id:
                    return message
            else:
                value = decode(message.value(), kafka.value_context(settings, name))
                if str(value["event_id"]) == event_id:
                    return message
    finally:
        consumer.close()
    raise AssertionError(f"event {event_id} never arrived on {name}")


def _spec(name: str, handler) -> ConsumerSpec:
    return ConsumerSpec(name=name, topics=("appointments.booked",), handler=handler)


async def _effects(factory, name: str) -> int:
    async with factory() as session:
        return await session.scalar(
            select(func.count()).select_from(ProcessedEvent)
            .where(ProcessedEvent.consumer == f"{name}-effect")
        )


def _recording(name: str, calls: list, *, fail_times: int = 0):
    async def handler(session, event, context):
        calls.append(event.event_id)
        if len(calls) <= fail_times:
            raise RuntimeError("transient failure")
        session.add(ProcessedEvent(consumer=f"{name}-effect", event_id=uuid.uuid4()))

    return handler


async def test_a_redelivered_event_is_applied_once(factory, backbone):
    """Referenced by tests/cases/evt-004-a-consumer-applies-an-event-once.yaml."""
    name = f"test-once-{uuid.uuid4().hex[:8]}"
    calls: list = []
    runner = ConsumerRunner(backbone, factory, _spec(name, _recording(name, calls)))
    message = await _booked_message(factory, backbone)
    assert await runner.handle(message) is Outcome.APPLIED
    assert await runner.handle(message) is Outcome.DUPLICATE
    assert await _effects(factory, name) == 1
    assert len(calls) == 1, "the handler must not even run for a duplicate"


async def test_a_transient_failure_is_retried_and_applied_once(factory, backbone):
    name = f"test-retry-{uuid.uuid4().hex[:8]}"
    calls: list = []
    runner = ConsumerRunner(
        backbone, factory, _spec(name, _recording(name, calls, fail_times=2)),
        backoff_seconds=0.01,
    )
    message = await _booked_message(factory, backbone)
    assert await runner.handle(message) is Outcome.APPLIED
    assert len(calls) == 3
    # The failed attempts' dedupe marks rolled back with them, or the third would have
    # been skipped as a duplicate and the event lost.
    assert await _effects(factory, name) == 1


async def test_a_poison_event_is_dead_lettered_and_the_partition_moves_on(factory, backbone):
    """Referenced by tests/cases/evt-005-a-poison-event-is-dead-lettered.yaml."""
    name = f"test-poison-{uuid.uuid4().hex[:8]}"
    calls: list = []
    runner = ConsumerRunner(
        backbone, factory, _spec(name, _recording(name, calls, fail_times=99)),
        backoff_seconds=0.01,
    )
    message = await _booked_message(factory, backbone)
    assert await runner.handle(message) is Outcome.DEAD_LETTERED
    assert len(calls) == 3
    assert await _effects(factory, name) == 0
    event_id = str(runner.decode(message).event_id)
    dead = json.loads(_fetch(backbone, "appointments.booked.dlt", event_id).value())
    assert dead["consumer"] == name
    assert "transient failure" in dead["error"]
    assert dead["attempts"] == 3
    # Not marked processed: a replay from the dead-letter topic will be applied.
    async with factory() as session:
        assert await session.get(ProcessedEvent, (name, uuid.UUID(event_id))) is None


async def test_a_replayed_dead_letter_is_applied(factory, backbone):
    """`dlt-replay` after the fix: the event comes back to its topic, and the consumer
    that dead-lettered it now applies it -- its dedupe mark was never written."""
    from app.events.deadletters import replay

    name = f"test-replay-{uuid.uuid4().hex[:8]}"
    failing = ConsumerRunner(
        backbone, factory, _spec(name, _recording(name, [], fail_times=99)),
        backoff_seconds=0.01,
    )
    message = await _booked_message(factory, backbone)
    assert await failing.handle(message) is Outcome.DEAD_LETTERED
    assert replay(backbone, "appointments.booked") >= 1

    event_id = failing.decode(message).event_id
    replayed = _fetch_latest(backbone, "appointments.booked", event_id, after=message.offset())
    fixed = ConsumerRunner(backbone, factory, _spec(name, _recording(name, [])))
    assert await fixed.handle(replayed) is Outcome.APPLIED
    assert await _effects(factory, name) == 1


def _fetch_latest(settings, name: str, event_id: str, *, after: int, timeout: float = 30):
    consumer = kafka.consumer(settings, f"fetch-{uuid.uuid4().hex[:8]}")
    topic_name = kafka.broker_topic(settings, name)
    parts = consumer.list_topics(topic_name, timeout=10).topics[topic_name].partitions
    consumer.assign([TopicPartition(topic_name, p, 0) for p in parts])
    decode = kafka.deserializer(kafka.schema_registry(settings))
    deadline = time.monotonic() + timeout
    seen = []
    try:
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is None or message.error():
                if seen:
                    return seen[-1]
                continue
            value = decode(message.value(), kafka.value_context(settings, name))
            if str(value["event_id"]) == event_id and message.offset() > after:
                seen.append(message)
    finally:
        consumer.close()
    raise AssertionError("the replayed copy never arrived")
