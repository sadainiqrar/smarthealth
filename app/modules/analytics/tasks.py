"""The nightly reconcile: the rollup checked against the system of record.

The rollup is built from events; if the consumer ever lost one, or counted one twice, a
dashboard would say something false and nobody would notice -- analytics is the one
surface where a wrong number looks exactly like a right one (ARCHITECTURE section 3.5).
Every night this recomputes the last `DAYS` days of booked, cancelled and completed from
scheduling's own tables and reports every (day, clinic, provider) where the two disagree.

It reports; it does not overwrite. An event still in flight at the moment of the check
would be counted by the consumer a second later, and a correction written now would then
double it. A drift that persists across nights is the real signal.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from opentelemetry import metrics
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.modules.scheduling.public as scheduling
from app.celery_app import TASKS, celery, task_settings
from app.core.clock import Clock
from app.modules.analytics.models import AnalyticsDaily
from app.settings import Settings

logger = logging.getLogger(__name__)
DAYS = 7
#: Events from the last hour may still be on their way; the check stops short of them.
SETTLE = timedelta(hours=1)

_drift = metrics.get_meter(__name__).create_counter(
    "smarthealth_analytics_drift", unit="{cell}",
    description="(day, clinic, provider, metric) cells where the rollup and the source disagree",
)


async def reconcile(
    settings: Settings, *, start: date | None = None, end: date | None = None
) -> list[dict]:
    """Every (day, clinic, provider, metric) where rollup and source disagree. The window
    defaults to the `DAYS` days that ended before `SETTLE` ago."""
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    end = end or (Clock().now() - SETTLE).date() - timedelta(days=1)
    start = start or end - timedelta(days=DAYS - 1)
    try:
        async with sessions() as session:
            truth = await scheduling.daily_counts(session, start=start, end=end)
            rows = (await session.scalars(
                select(AnalyticsDaily).where(AnalyticsDaily.day >= start,
                                             AnalyticsDaily.day <= end)
            )).all()
    finally:
        await engine.dispose()
    rolled = {name: {} for name in truth}
    for row in rows:
        for name in truth:
            rolled[name][(row.day, row.clinic_id, row.provider_id)] = getattr(row, name)
    drift = []
    for name, counts in truth.items():
        for key in set(counts) | set(rolled[name]):
            expected, actual = counts.get(key, 0), rolled[name].get(key, 0)
            if expected != actual:
                drift.append({"metric": name, "day": key[0].isoformat(), "clinic_id": str(key[1]),
                              "provider_id": str(key[2]), "expected": expected,
                              "actual": actual})
    for cell in drift:
        logger.warning("analytics drift", extra=cell)
        _drift.add(1, {"metric": cell["metric"]})
    return drift


@celery.task(name="analytics.reconcile", acks_late=True)
def reconcile_analytics() -> int:
    return len(asyncio.run(reconcile(task_settings())))


TASKS.register("analytics.reconcile", reconcile_analytics)
