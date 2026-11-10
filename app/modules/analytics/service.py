"""Writing and reading the daily rollup.

`record` is the consumer's one write: an upsert that adds to a day's counters, in the
caller's transaction. Reads aggregate the rollup over a window -- never the source tables,
which belong to scheduling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.models import AnalyticsDaily

COUNTERS = ("booked", "cancelled", "completed", "no_show", "wait_seconds_sum", "wait_count")
METRICS = ("booked", "cancelled", "completed", "no_show", "average_wait_seconds")


async def record(
    session: AsyncSession, *, day: date, clinic_id: uuid.UUID, provider_id: uuid.UUID,
    **increments: int,
) -> None:
    """Add `increments` to the day's row for this clinic and provider, creating it."""
    unknown = set(increments) - set(COUNTERS)
    if unknown:
        raise ValueError(f"not analytics counters: {sorted(unknown)}")
    values = {name: increments.get(name, 0) for name in COUNTERS}
    statement = insert(AnalyticsDaily).values(
        day=day, clinic_id=clinic_id, provider_id=provider_id, **values
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[AnalyticsDaily.day, AnalyticsDaily.clinic_id,
                            AnalyticsDaily.provider_id],
            set_={name: getattr(AnalyticsDaily, name) + getattr(statement.excluded, name)
                  for name in increments},
        )
    )


@dataclass(frozen=True)
class Summary:
    booked: int
    cancelled: int
    completed: int
    no_show: int
    #: cancelled / booked over the window; None when nothing was booked.
    cancellation_rate: float | None
    #: seen_at - checked_in_at, averaged over the visits that were seen; None if none were.
    average_wait_seconds: float | None


def _window(statement, start: date, end: date, clinic_id: uuid.UUID | None):
    statement = statement.where(AnalyticsDaily.day >= start, AnalyticsDaily.day <= end)
    if clinic_id is not None:
        statement = statement.where(AnalyticsDaily.clinic_id == clinic_id)
    return statement


async def summary(
    session: AsyncSession, *, start: date, end: date, clinic_id: uuid.UUID | None = None
) -> Summary:
    totals = (await session.execute(_window(
        select(*(func.coalesce(func.sum(getattr(AnalyticsDaily, name)), 0) for name in COUNTERS)),
        start, end, clinic_id,
    ))).one()
    booked, cancelled, completed, no_show, wait_sum, wait_count = (int(v) for v in totals)
    return Summary(
        booked=booked, cancelled=cancelled, completed=completed, no_show=no_show,
        cancellation_rate=(cancelled / booked) if booked else None,
        average_wait_seconds=(wait_sum / wait_count) if wait_count else None,
    )


async def timeseries(
    session: AsyncSession, *, metric: str, start: date, end: date,
    clinic_id: uuid.UUID | None = None,
) -> list[tuple[date, float | None]]:
    """One point per day that has data, oldest first."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    if metric == "average_wait_seconds":
        value = func.sum(AnalyticsDaily.wait_seconds_sum) / func.nullif(
            func.sum(AnalyticsDaily.wait_count), 0
        )
    else:
        value = func.sum(getattr(AnalyticsDaily, metric))
    rows = await session.execute(_window(
        select(AnalyticsDaily.day, value).group_by(AnalyticsDaily.day).order_by(AnalyticsDaily.day),
        start, end, clinic_id,
    ))
    return [(day, float(total) if total is not None else None) for day, total in rows]
