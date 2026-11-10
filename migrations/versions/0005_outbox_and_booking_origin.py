"""the transactional outbox, the consumers' dedupe table, and a booking's origin

`outbox_events`: every domain event is written here in the same transaction as the
change it describes, and published to Kafka by the relay afterwards (Weeks 3-5 spec
D-21). Ordered by `seq`, an identity column, because per-entity order matters and two
uuid7 ids minted in one millisecond do not sort reliably. The partial index holds only
the unpublished backlog -- the relay's only query.

`processed_events`: per consumer, the events it has applied, inserted in the same
transaction as the side effect (harness requirement 4).

`appointments.origin` / `conversation_ref`: where a booking came from, carried on
`appointments.booked` (spec section 3.2). Nullable; existing rows have no origin.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("producer", sa.String(length=50), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "headers", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_outbox_events")),
        sa.UniqueConstraint("event_id", name=op.f("uq_outbox_events_event_id")),
    )
    op.create_index(
        "ix_outbox_events_unpublished", "outbox_events", ["seq"], unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(length=200), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_processed_events")),
    )
    op.add_column("appointments", sa.Column("origin", sa.String(length=32), nullable=True))
    op.add_column(
        "appointments", sa.Column("conversation_ref", sa.String(length=200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("appointments", "conversation_ref")
    op.drop_column("appointments", "origin")
    op.drop_table("processed_events")
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")
