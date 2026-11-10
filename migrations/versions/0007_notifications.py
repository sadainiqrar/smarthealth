"""notifications: one row per message to a patient

Written in the same transaction as the reason for it, and sent by a Celery task enqueued
after the commit (Weeks 3-5 spec section 5.1). `idempotency_key` is unique so one reason
queues one message; the send task claims a row (`queued` -> `sending`) before sending, so
a twice-delivered task sends once.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("channel", sa.Enum("email", "sms", name="channel", native_enum=False,
                                     create_constraint=True, length=8), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Enum("queued", "sending", "sent", "failed",
                                    name="notificationstatus", native_enum=False,
                                    create_constraint=True, length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_notifications_idempotency_key")),
    )
    op.create_index(op.f("ix_notifications_kind"), "notifications", ["kind"])
    op.create_index(op.f("ix_notifications_patient_id"), "notifications", ["patient_id"])
    op.create_index(op.f("ix_notifications_status"), "notifications", ["status"])
    op.execute(
        """
        CREATE TRIGGER trg_notifications_updated_at
        BEFORE UPDATE ON notifications
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_notifications_updated_at ON notifications")
    op.drop_index(op.f("ix_notifications_status"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_patient_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_kind"), table_name="notifications")
    op.drop_table("notifications")
