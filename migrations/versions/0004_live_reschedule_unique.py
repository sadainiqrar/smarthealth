"""at most one live replacement per appointment

A partial unique index on `appointments (rescheduled_from_id)`, over live rows only:
at most one live appointment replaces any given appointment.

Without it two reschedules of one appointment, accepted together, each ran their own
booking for a different new slot. Both confirmed, the first `complete_reschedule` moved
the old appointment to `rescheduled`, the second found it already `rescheduled` and
answered as a retry -- and the patient held two confirmed appointments for the one they
asked to move. Every existing constraint was satisfied throughout: the two new rows held
different slots. The router's precheck cannot close this; both requests pass it before
either workflow writes anything.

With the index the second booking's `create_pending` fails on insert while the first
replacement is pending or confirmed, so it never claims a slot and its compensation is a
no-op. Partial on status, like `ux_appointments_live_slot`: a replacement that failed or
was cancelled no longer blocks another attempt (PRD D-5).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # At most one *live* appointment may replace a given appointment. The model declares
    # the same index (`Appointment.__table_args__`) so `alembic check` sees it as managed.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_appointments_live_reschedule
            ON appointments (rescheduled_from_id)
         WHERE status IN ('pending', 'confirmed') AND rescheduled_from_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_appointments_live_reschedule")
