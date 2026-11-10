"""analytics: the daily rollup, and a read-only role for Grafana

`analytics_daily` is what the five Part A metrics are read from (PART-A-FR-5), one row
per (day, clinic, provider), incremented by the analytics consumer in the same
transaction as its processed_events mark.

`analytics_reader`: Grafana's Postgres datasource logs in as this role and can SELECT the
rollup and count patients -- nothing else, and never write. Cluster-level, so created only
if missing (every test run's database shares the server). The password is a development
one, like every credential in compose; a deployment sets its own.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booked", sa.Integer(), nullable=False),
        sa.Column("cancelled", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False),
        sa.Column("no_show", sa.Integer(), nullable=False),
        sa.Column("wait_seconds_sum", sa.BigInteger(), nullable=False),
        sa.Column("wait_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day", "clinic_id", "provider_id",
                                name=op.f("pk_analytics_daily")),
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_reader') THEN
                CREATE ROLE analytics_reader LOGIN PASSWORD 'analytics-reader';
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT SELECT ON analytics_daily, patients TO analytics_reader")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON analytics_daily, patients FROM analytics_reader")
    op.drop_table("analytics_daily")
