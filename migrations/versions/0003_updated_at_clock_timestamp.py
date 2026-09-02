"""updated_at uses clock_timestamp, not now()

`now()` is transaction_timestamp() - frozen for the whole transaction, so an
update inside a long transaction was backdated to when that transaction began.
`updated_at` exists to record when a row actually changed, so it needs the
wall-clock instant. `created_at` keeps now() deliberately: creation belongs to
the logical operation, and rows inserted together sharing a timestamp is right.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `now()` is transaction_timestamp() — frozen for the whole transaction, so an
    # update inside a long transaction is backdated to when that transaction began.
    # `updated_at` exists to record when a row actually changed, so it needs the
    # wall-clock instant. `created_at` keeps now() deliberately: creation belongs to
    # the logical operation, and rows inserted together sharing a timestamp is right.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
