"""billing records: one per appointment, a state machine over the payment gateway

`billing_records` replaces the Week 2 stand-ins (spec D-27): the booking pre-check places
an authorisation, compensation, cancellation and reschedule release it (void or refund),
and the post-visit workflow captures it. One row per appointment (unique), status as
VARCHAR + CHECK like every other enum here, and the gateway's authorisation id so a
repeated operation can name what it acts on.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("authorized", "declined", "captured", "voided", "refunded")


def upgrade() -> None:
    op.create_table(
        "billing_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*STATUSES, name="billingstatus", native_enum=False, create_constraint=True,
                    length=16),
            nullable=False,
        ),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default=sa.text("'USD'"),
                  nullable=False),
        sa.Column("authorization_ref", sa.String(length=100), nullable=True),
        sa.Column("decline_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["appointments.id"],
            name=op.f("fk_billing_records_appointment_id_appointments"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_records")),
        sa.UniqueConstraint("appointment_id", name=op.f("uq_billing_records_appointment_id")),
    )
    op.create_index(op.f("ix_billing_records_status"), "billing_records", ["status"])
    # The repository invariant: updated_at moves on raw SQL updates too (0001, 0003).
    op.execute(
        """
        CREATE TRIGGER trg_billing_records_updated_at
        BEFORE UPDATE ON billing_records
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_billing_records_updated_at ON billing_records")
    op.drop_index(op.f("ix_billing_records_status"), table_name="billing_records")
    op.drop_table("billing_records")
