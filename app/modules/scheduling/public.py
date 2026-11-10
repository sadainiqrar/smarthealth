"""The scheduling module's published interface (the D-15 pattern).

Analytics' nightly reconcile reads scheduling's own counts here, to check the rollup it
built from events against the system of record -- never the tables themselves.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import cast, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date

from app.modules.scheduling.models import Appointment, AppointmentStatus, Visit

Key = tuple[date, uuid.UUID, uuid.UUID]


async def daily_counts(
    session: AsyncSession, *, start: date, end: date
) -> dict[str, Counter[Key]]:
    """Per (UTC day, clinic, provider), between `start` and `end` inclusive: confirmed
    bookings (by `confirmed_at`), cancellations (by `cancelled_at`) and completed visits
    (by `completed_at`) -- the facts the rollup's booked, cancelled and completed come from."""
    lower = datetime.combine(start, time.min, tzinfo=timezone.utc)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)

    def day(column):
        # A literal, not a bound parameter: GROUP BY must see the very expression the
        # SELECT does, and two binds of 'UTC' are two different expressions to Postgres.
        return cast(func.timezone(literal_column("'UTC'"), column), Date)

    async def counted(day_column, *where) -> Counter[Key]:
        rows = await session.execute(
            select(day(day_column), Appointment.clinic_id, Appointment.provider_id, func.count())
            .select_from(Appointment)
            .where(day_column >= lower, day_column < upper, *where)
            .group_by(day(day_column), Appointment.clinic_id, Appointment.provider_id)
        )
        return Counter({(d, c, p): n for d, c, p, n in rows})

    booked = await counted(Appointment.confirmed_at, Appointment.confirmed_at.is_not(None))
    cancelled = await counted(
        Appointment.cancelled_at, Appointment.status == AppointmentStatus.CANCELLED
    )
    rows = await session.execute(
        select(day(Visit.completed_at), Appointment.clinic_id, Appointment.provider_id,
               func.count())
        .join(Appointment, Appointment.id == Visit.appointment_id)
        .where(Visit.completed_at >= lower, Visit.completed_at < upper)
        .group_by(day(Visit.completed_at), Appointment.clinic_id, Appointment.provider_id)
    )
    completed = Counter({(d, c, p): n for d, c, p, n in rows})
    return {"booked": booked, "cancelled": cancelled, "completed": completed}
