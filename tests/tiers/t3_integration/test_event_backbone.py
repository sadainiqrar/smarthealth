"""The outbox, the relay and Kafka, against real Postgres, Kafka and Schema Registry.

What these prove, in the order an event lives:

1. the event is written in the same transaction as the change -- a rollback leaves
   neither (the property that closes PRD R-17);
2. the relay publishes it, Avro-encoded under its registered schema, keyed by its
   subject, with the trace context of the transaction that wrote it;
3. a relay that dies after sending but before marking the row sends it again
   (at-least-once), and per-key order holds;
4. a schema change that would break old consumers is refused by Schema Registry.

Topics and subjects carry this run's prefix (harness requirement 1), so the broker and
the registry are shared with other runs safely.
"""

from __future__ import annotations

import time
import uuid

import pytest
from confluent_kafka import TopicPartition
from confluent_kafka.schema_registry import Schema
from confluent_kafka.schema_registry.error import SchemaRegistryError
from sqlalchemy import select

from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events import kafka, schemas
from app.events.models import OutboxEvent
from app.events.relay import Relay
from app.modules.scheduling import service
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]

READ_TIMEOUT_SECONDS = 30


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


def _read(
    settings, name: str, event_ids: set[str], timeout: float = READ_TIMEOUT_SECONDS,
    at_least: int | None = None,
):
    """Messages on `name` whose event id is in `event_ids`, decoded, in log order: until
    `at_least` of them have arrived (default: one per id) or the timeout passes."""
    wanted = at_least if at_least is not None else len(event_ids)
    consumer = kafka.consumer(settings, f"test-reader-{uuid.uuid4().hex[:8]}")
    topic_name = kafka.broker_topic(settings, name)
    metadata = consumer.list_topics(topic_name, timeout=10)
    partitions = [TopicPartition(topic_name, p, 0) for p in metadata.topics[topic_name].partitions]
    consumer.assign(partitions)
    decode = kafka.deserializer(kafka.schema_registry(settings))
    found: list[tuple[object, dict]] = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and len(found) < wanted:
            message = consumer.poll(1.0)
            if message is None or message.error():
                continue
            value = decode(message.value(), kafka.value_context(settings, name))
            # Avro's uuid logical type decodes to uuid.UUID; compare as text.
            if str(value["event_id"]) in event_ids:
                found.append((message, value))
    finally:
        consumer.close()
    return found


async def _events_for(factory, topic: str, key: str) -> list[OutboxEvent]:
    async with factory() as session:
        return (
            await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.topic == topic, OutboxEvent.key == key)
                .order_by(OutboxEvent.seq)
            )
        ).all()


async def test_a_cancel_writes_its_event_in_the_same_transaction(factory):
    """Referenced by tests/cases/evt-001-an-event-commits-with-its-change.yaml.

    The R-17 bug was a follow-up started after the commit and lost if that start failed.
    An outbox row cannot be lost that way: it is part of the commit."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    key = str(appointment_id)
    assert len(await _events_for(factory, "appointments.booked", key)) == 1

    async with factory() as session:
        await service.cancel_appointment(
            session, audit, appointment_id=appointment_id, reason="rolled back",
            now=Clock().now(), actor_user_id="u1",
        )
        await session.rollback()
    assert await _events_for(factory, "appointments.cancelled", key) == []

    async with factory() as session:
        await service.cancel_appointment(
            session, audit, appointment_id=appointment_id, reason="moving away",
            now=Clock().now(), actor_user_id="u1",
        )
        await session.commit()
    (event,) = await _events_for(factory, "appointments.cancelled", key)
    assert event.payload["reason"] == "moving away"
    assert event.payload["initiated_by"] == "u1"
    assert event.published_at is None


async def test_the_relay_publishes_avro_keyed_by_subject_in_order(factory, backbone):
    """Referenced by tests/cases/evt-002-the-relay-publishes-in-order.yaml."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    async with factory() as session:
        await service.cancel_appointment(
            session, audit, appointment_id=appointment_id, reason="r", now=Clock().now(),
            actor_user_id=None,
        )
        await session.commit()
    key = str(appointment_id)
    booked = (await _events_for(factory, "appointments.booked", key))[0]
    cancelled = (await _events_for(factory, "appointments.cancelled", key))[0]

    relay = Relay(backbone, factory)
    while (await relay.run_once()).published:
        pass

    async with factory() as session:
        for row in (booked, cancelled):
            assert (await session.get(OutboxEvent, row.seq)).published_at is not None

    (message, value), = _read(backbone, "appointments.booked", {str(booked.event_id)})
    assert message.key().decode() == key
    assert value["subject_id"] == key and value["event_type"] == "appointments.booked"
    assert str(value["payload"]["appointment_id"]) == key
    assert value["occurred_at"] is not None
    (cancel_message, _), = _read(backbone, "appointments.cancelled", {str(cancelled.event_id)})
    # One key, one partition: a consumer reading appointments.* sees an appointment's
    # story in order.
    assert cancel_message.key().decode() == key


async def test_a_relay_that_dies_before_marking_sends_again(factory, backbone):
    """At least once: the broker received it, the row was never marked, so it goes again.
    Consumers dedupe on event_id; losing it instead would be the bug."""
    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    (row,) = await _events_for(factory, "appointments.booked", str(appointment_id))

    class _DiesBeforeCommit(Relay):
        async def run_once(self):  # produce and flush, then "crash"
            async with self._sessions() as session:
                pending = await session.get(OutboxEvent, row.seq)
                self._producer.produce(
                    kafka.broker_topic(self._settings, pending.topic),
                    key=pending.key.encode(), value=self._value(pending),
                )
                self._producer.flush(10)
                await session.rollback()

    await _DiesBeforeCommit(backbone, factory).run_once()
    relay = Relay(backbone, factory)
    while (await relay.run_once()).published:
        pass
    copies = _read(backbone, "appointments.booked", {str(row.event_id)}, at_least=2)
    assert len(copies) >= 2, "the unmarked row must be published again"
    assert {str(value["event_id"]) for _, value in copies} == {str(row.event_id)}


async def test_a_payload_that_breaks_its_schema_rolls_the_change_back(factory):
    """An event nobody can serialise must not be the reason a change looks done: emit
    validates, so the business write fails with it."""
    from app.events.outbox import emit

    async with factory() as session:
        with pytest.raises(schemas.InvalidEvent):
            emit(session, "appointments.cancelled", key="k", payload={"appointment_id": "x"})


async def test_schema_registry_refuses_a_breaking_change(backbone):
    """Referenced by tests/cases/evt-003-a-breaking-schema-change-is-refused.yaml.

    BACKWARD_TRANSITIVE: removing a field without a default would leave consumers on the
    new schema unable to read old events. The registry rejects it at deploy time."""
    import json

    client = kafka.schema_registry(backbone)
    subject = kafka.subject(backbone, "appointments.cancelled")
    current = json.loads(schemas.schema_text("appointments.cancelled"))
    payload = current["fields"][-1]["type"]
    payload["fields"].append({"name": "required_newcomer", "type": "string"})
    broken = Schema(json.dumps(current), "AVRO")
    assert client.test_compatibility(subject, broken) is False
    with pytest.raises(SchemaRegistryError):
        client.register_schema(subject, broken)


async def test_the_published_event_continues_the_writers_trace(factory, backbone, spans):
    """The outbox is a hop through a table; without the stored traceparent every trace
    would end there, and a consumer's work could never be tied to the request."""
    from opentelemetry import trace

    audit = RecordingAudit()
    _, appointment_id = await confirmed_appointment(factory, audit)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("cancel request") as request_span:
        async with factory() as session:
            await service.cancel_appointment(
                session, audit, appointment_id=appointment_id, reason="r",
                now=Clock().now(), actor_user_id=None,
            )
            await session.commit()
    writer_trace = request_span.get_span_context().trace_id

    relay = Relay(backbone, factory)
    while (await relay.run_once()).published:
        pass
    publishes = [
        s for s in spans.get_finished_spans()
        if s.name == "publish appointments.cancelled" and s.context.trace_id == writer_trace
    ]
    assert publishes, "the relay's publish span is not in the request's trace"
