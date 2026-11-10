"""The outbox relay: unpublished rows to Kafka, in order, at least once.

One loop, its own process (`python -m app.relay`, the `relay` compose service). Each
round, in one transaction:

1. lock the oldest unpublished rows -- `FOR UPDATE SKIP LOCKED`, so a second relay
   replica takes different rows instead of waiting or double-sending;
2. produce each to its topic, keyed by its subject, Avro-encoded with its registered
   schema, carrying the trace context stored on the row;
3. wait for the broker's delivery reports, mark the delivered rows published, record the
   error on the rest, commit.

A crash between 2 and 3 sends those rows again on the next round. That is the
at-least-once contract every consumer is built for: they dedupe on `event_id`. Order per
key holds because rows are taken in `seq` order and one key always maps to one partition.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from confluent_kafka import KafkaError, Message, Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.serialization import StringSerializer
from opentelemetry import metrics, propagate, trace
from opentelemetry.trace import SpanKind
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.events import kafka, schemas
from app.events.models import OutboxEvent
from app.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

BATCH_SIZE = 500
IDLE_SECONDS = 0.5
DELIVERY_TIMEOUT_SECONDS = 30
#: Recorded errors are truncated; the full one is in the relay's log.
ERROR_LIMIT = 2000

_published = meter.create_counter(
    "smarthealth_outbox_published", unit="{event}", description="Outbox rows delivered"
)
_failed = meter.create_counter(
    "smarthealth_outbox_failed", unit="{event}", description="Outbox rows Kafka refused"
)
_backlog_age = {"seconds": 0.0}
meter.create_observable_gauge(
    "smarthealth_outbox_oldest_unpublished_age",
    callbacks=[lambda _options: [metrics.Observation(_backlog_age["seconds"])]],
    unit="s",
    description="Age of the oldest event not yet on Kafka; the event system's health",
)


@dataclass
class RoundResult:
    published: int = 0
    failed: int = 0


class Relay:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        producer: Producer | None = None,
        registry: SchemaRegistryClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._producer = producer or kafka.producer(settings)
        self._registry = registry or kafka.schema_registry(settings)
        self._serializers: dict[str, object] = {}
        self._keys = StringSerializer("utf_8")
        self._clock = clock or Clock()

    def _serializer(self, name: str):
        if name not in self._serializers:
            self._serializers[name] = kafka.serializer(self._settings, self._registry, name)
        return self._serializers[name]

    def _value(self, row: OutboxEvent) -> bytes:
        record = {
            "event_id": str(row.event_id),
            "event_type": row.topic,
            "schema_version": row.schema_version,
            "occurred_at": row.created_at,
            "producer": row.producer,
            "subject_id": row.key,
            "payload": row.payload,
        }
        value = schemas.from_json(schemas.parsed_schema(row.topic), schemas.to_json(record))
        return self._serializer(row.topic)(value, kafka.value_context(self._settings, row.topic))

    async def run_once(self) -> RoundResult:
        """One round: publish up to BATCH_SIZE rows. Returns what happened."""
        result = RoundResult()
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.seq)
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                await self._measure_backlog(session)
                return result

            outcomes: dict[int, KafkaError | None] = {}
            # A key whose row could not be produced holds back its later rows this round,
            # so a consumer never sees an entity's events out of order.
            blocked: set[str] = set()
            attempted: list[OutboxEvent] = []
            for row in rows:
                if row.key in blocked:
                    continue
                attempted.append(row)
                parent = propagate.extract(row.headers or {})
                with tracer.start_as_current_span(
                    f"publish {row.topic}", context=parent, kind=SpanKind.PRODUCER,
                    attributes={
                        "messaging.system": "kafka",
                        "messaging.destination.name": kafka.broker_topic(self._settings, row.topic),
                        "messaging.message.id": str(row.event_id),
                    },
                ):
                    headers: dict[str, str] = {}
                    propagate.inject(headers)
                    try:
                        value = self._value(row)
                    except Exception as exc:  # noqa: BLE001 - recorded on the row, then retried
                        outcomes[row.seq] = KafkaError(KafkaError._VALUE_SERIALIZATION, str(exc))
                        blocked.add(row.key)
                        continue
                    self._producer.produce(
                        kafka.broker_topic(self._settings, row.topic),
                        key=self._keys(row.key),
                        value=value,
                        headers=list(headers.items()),
                        on_delivery=_recorder(outcomes, row.seq),
                    )
            remaining = await asyncio.to_thread(self._producer.flush, DELIVERY_TIMEOUT_SECONDS)
            now = self._clock.now()
            for row in attempted:
                if row.seq not in outcomes and remaining:
                    outcomes[row.seq] = KafkaError(KafkaError._MSG_TIMED_OUT, "no delivery report")
                error = outcomes.get(row.seq)
                row.attempts += 1
                if error is None:
                    row.published_at = now
                    row.last_error = None
                    result.published += 1
                else:
                    row.last_error = str(error)[:ERROR_LIMIT]
                    result.failed += 1
                    logger.warning(
                        "outbox row not published",
                        extra={"seq": row.seq, "topic": row.topic, "error": str(error)},
                    )
            await session.commit()
        _published.add(result.published)
        _failed.add(result.failed)
        async with self._sessions() as session:
            await self._measure_backlog(session)
        return result

    async def _measure_backlog(self, session: AsyncSession) -> None:
        oldest = await session.scalar(
            select(func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
        )
        _backlog_age["seconds"] = (
            max((self._clock.now() - oldest).total_seconds(), 0.0) if oldest else 0.0
        )

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                outcome = await self.run_once()
            except Exception:
                logger.exception("relay round failed; retrying")
                outcome = RoundResult(failed=1)
            if outcome.published == 0 or outcome.failed:
                # Idle, or the broker is refusing: back off instead of spinning.
                elapsed = time.monotonic() - started
                try:
                    await asyncio.wait_for(stop.wait(), max(IDLE_SECONDS - elapsed, 0.05))
                except TimeoutError:
                    pass


def _recorder(outcomes: dict[int, KafkaError | None], seq: int):
    def on_delivery(error: KafkaError | None, _message: Message) -> None:
        outcomes[seq] = error

    return on_delivery
