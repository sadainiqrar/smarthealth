"""The outbox and the consumers' dedupe table.

`outbox_events` is written by every producer in its own transaction and drained by the
relay. `processed_events` records, per consumer, the events it has applied -- inserted in
the same transaction as the consumer's side effect, so "applied" and "recorded as
applied" cannot disagree (harness requirement 4). A Redis mark could.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    #: Publication order. Not the uuid7 event id: two events for one appointment written in
    #: the same millisecond could sort either way by id, and per-entity order is what a
    #: consumer of `appointments.*` relies on.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    #: The envelope's `event_id`, every consumer's idempotency key.
    event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), unique=True)
    #: Logical topic name (`appointments.cancelled`); the relay namespaces it.
    topic: Mapped[str] = mapped_column(String(200))
    #: The Kafka message key and the envelope's `subject_id`.
    key: Mapped[str] = mapped_column(String(200))
    producer: Mapped[str] = mapped_column(String(50))
    schema_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    #: W3C trace context of the transaction that wrote the row, so the relay continues
    #: that trace instead of starting one nothing links back to.
    headers: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    #: When the change was written: clock_timestamp() at insert. The envelope's
    #: `occurred_at`.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # The relay's only query: the oldest unpublished rows. Partial, so the index holds
        # the backlog only, not every event ever sent.
        Index(
            "ix_outbox_events_unpublished",
            "seq",
            postgresql_where=text("published_at IS NULL"),
        ),
    )


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
