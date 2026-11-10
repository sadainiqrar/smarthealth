"""Writing an event: one row in `outbox_events`, in the caller's transaction (D-21).

This is the only way an event enters the system. A business change and a Kafka publish
cannot be made atomic -- publishing after the commit loses the event on a crash, which is
exactly PRD R-17 -- but a business change and an INSERT into the same database can. The
relay (`app.events.relay`) publishes the row afterwards; if it is down, events wait in
the table, and nothing is lost.

The caller owns the transaction: `emit` adds the row to the session and never commits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from opentelemetry import propagate
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.events import catalog, schemas
from app.events.models import OutboxEvent

PRODUCER = "part-a"

#: Stands in for `occurred_at` during validation only. The real value is the row's insert
#: time, known only after the INSERT, and any aware timestamp validates the same.
_VALIDATION_TIME = datetime(2000, 1, 1, tzinfo=UTC)


def _envelope_for_validation(entry: catalog.Topic, key: str, payload: dict[str, Any]):
    return {
        "event_id": str(uuid7()),
        "event_type": entry.name,
        "schema_version": entry.schema_version,
        "occurred_at": _VALIDATION_TIME,
        "producer": PRODUCER,
        "subject_id": key,
        "payload": payload,
    }


def emit(session: AsyncSession, topic: str, *, key: str, payload: dict[str, Any]) -> OutboxEvent:
    """Add an event for `topic` about `key` to the caller's transaction.

    Validates `payload` against the topic's schema first and raises
    `schemas.InvalidEvent` if it does not fit, which rolls the business change back with
    it -- an event nobody can serialise must not be the reason a change looks done.
    """
    entry = catalog.topic(topic)
    schemas.check(topic, _envelope_for_validation(entry, key, payload))
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    row = OutboxEvent(
        event_id=uuid7(),
        topic=topic,
        key=key,
        producer=PRODUCER,
        schema_version=entry.schema_version,
        payload=schemas.to_json(payload),
        headers=carrier,
    )
    session.add(row)
    return row
