"""The consumer runtime: decode, dedupe, apply, retry, dead-letter, then commit the offset.

A consumer is a name, the topics it reads and an async handler `(session, event)`. The
runtime gives every handler the same guarantees, so no handler re-implements them:

- **At most one application per event.** Before the handler runs, the runtime inserts
  `(consumer, event_id)` into `processed_events` in the handler's own transaction. A
  redelivered event hits the primary key and is skipped; a handler that fails rolls the
  insert back with its side effect, so the event is tried again. Harness requirement 4:
  the idempotency key is explicit (`event_id`), never inferred.
- **Offsets after the commit.** The offset is committed only once the handler's
  transaction has, so a crash in between redelivers rather than skips. At least once in,
  at most once applied.
- **A poison message cannot stall a partition.** After `max_attempts` the message goes to
  `<topic>.dlt` with the error, and the offset moves on. `python -m app.cli dlt-replay`
  re-publishes dead letters once the cause is fixed.
- **One trace.** The handler runs in a span whose parent is the trace context the relay
  put in the message headers.

Consumers register in `CONSUMERS` (harness requirement 3), and the
`test_consumers_idempotent` meta-test delivers each registered consumer's fixture event
twice and asserts one side effect.
"""

from __future__ import annotations

import asyncio
import base64
import enum
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from confluent_kafka import Consumer, Message, Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from opentelemetry import metrics, propagate, trace
from opentelemetry.trace import SpanKind
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.registry import Registry
from app.events import kafka, schemas
from app.events.models import ProcessedEvent
from app.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_outcomes = meter.create_counter(
    "smarthealth_consumer_events", unit="{event}",
    description="Events handled, by consumer and outcome (applied, duplicate, dead_lettered)",
)

@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    subject_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    topic: str
    partition: int
    offset: int


Handler = Callable[[AsyncSession, Event], Awaitable[None]]


@dataclass(frozen=True)
class ConsumerSpec:
    name: str
    topics: tuple[str, ...]
    handler: Handler
    #: Events for the meta-test: one valid event per topic, as a handler would get it.
    fixtures: tuple[dict[str, Any], ...] = field(default=())


CONSUMERS: Registry[ConsumerSpec] = Registry("consumer")


class Outcome(enum.Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    DEAD_LETTERED = "dead_lettered"


class ConsumerRunner:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        spec: ConsumerSpec,
        *,
        registry: SchemaRegistryClient | None = None,
        dead_letters: Producer | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self.spec = spec
        self._decode = kafka.deserializer(registry or kafka.schema_registry(settings))
        self._dead_letters = dead_letters or kafka.producer(settings)
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._logical = {kafka.broker_topic(settings, name): name for name in spec.topics}

    def kafka_consumer(self) -> Consumer:
        consumer = kafka.consumer(self._settings, self.spec.name)
        consumer.subscribe(list(self._logical))
        return consumer

    def decode(self, message: Message) -> Event:
        logical = self._logical[message.topic()]
        value = self._decode(message.value(), kafka.value_context(self._settings, logical))
        return Event(
            event_id=str(value["event_id"]),
            event_type=value["event_type"],
            subject_id=value["subject_id"],
            occurred_at=value["occurred_at"],
            payload=schemas.to_json(value["payload"]),
            topic=logical,
            partition=message.partition(),
            offset=message.offset(),
        )

    async def apply(self, event: Event) -> Outcome:
        """Dedupe and run the handler in one transaction. Raises if the handler fails."""
        async with self._sessions() as session:
            inserted = await session.scalar(
                pg_insert(ProcessedEvent)
                .values(consumer=self.spec.name, event_id=event.event_id)
                .on_conflict_do_nothing()
                .returning(ProcessedEvent.event_id)
            )
            if inserted is None:
                await session.rollback()
                return Outcome.DUPLICATE
            try:
                await self.spec.handler(session, event)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return Outcome.APPLIED

    async def handle(self, message: Message) -> Outcome:
        """One message, to a final outcome. Never raises for a handler's failure."""
        carrier = {key: value.decode() for key, value in (message.headers() or [])}
        with tracer.start_as_current_span(
            f"consume {self._logical.get(message.topic(), message.topic())}",
            context=propagate.extract(carrier), kind=SpanKind.CONSUMER,
            attributes={"messaging.system": "kafka", "messaging.consumer.group.name":
                        self.spec.name, "messaging.destination.name": message.topic()},
        ) as span:
            try:
                event = self.decode(message)
            except Exception as exc:  # noqa: BLE001 - undecodable: retrying cannot help
                return self._dead_letter(message, None, exc, attempts=0)
            span.set_attribute("messaging.message.id", event.event_id)
            error: BaseException | None = None
            for attempt in range(1, self._max_attempts + 1):
                try:
                    outcome = await self.apply(event)
                    _outcomes.add(1, {"consumer": self.spec.name, "outcome": outcome.value})
                    return outcome
                except Exception as exc:  # noqa: BLE001 - retried, then dead-lettered
                    error = exc
                    logger.warning(
                        "consumer handler failed",
                        extra={"consumer": self.spec.name, "event_id": event.event_id,
                               "attempt": attempt, "error": repr(exc)},
                    )
                    if attempt < self._max_attempts:
                        await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            assert error is not None
            span.record_exception(error)
            return self._dead_letter(message, event, error, attempts=self._max_attempts)

    def _dead_letter(
        self, message: Message, event: Event | None, error: BaseException, *, attempts: int
    ) -> Outcome:
        logical = self._logical.get(message.topic(), message.topic())
        body = {
            "consumer": self.spec.name,
            "topic": logical,
            "partition": message.partition(),
            "offset": message.offset(),
            "attempts": attempts,
            "error": repr(error)[:2000],
            "event": schemas.to_json(event.__dict__) if event else None,
            # The exact bytes, so `dlt-replay` re-publishes what was sent, not a re-encoding.
            "value_b64": base64.b64encode(message.value() or b"").decode(),
            "key": (message.key() or b"").decode(errors="replace"),
        }
        headers: dict[str, str] = {}
        propagate.inject(headers)
        self._dead_letters.produce(
            kafka.broker_topic(self._settings, f"{logical}.dlt"),
            key=message.key(), value=json.dumps(body).encode(),
            headers=list(headers.items()),
        )
        self._dead_letters.flush(10)
        logger.error(
            "event dead-lettered",
            extra={"consumer": self.spec.name, "topic": logical, "offset": message.offset()},
        )
        _outcomes.add(1, {"consumer": self.spec.name, "outcome": Outcome.DEAD_LETTERED.value})
        return Outcome.DEAD_LETTERED

    async def run_forever(self, stop: asyncio.Event) -> None:
        consumer = self.kafka_consumer()
        try:
            while not stop.is_set():
                message = await asyncio.to_thread(consumer.poll, 1.0)
                if message is None:
                    continue
                if message.error():
                    logger.warning("consumer poll error", extra={"error": str(message.error())})
                    continue
                await self.handle(message)
                await asyncio.to_thread(consumer.commit, message=message, asynchronous=False)
        finally:
            await asyncio.to_thread(consumer.close)
