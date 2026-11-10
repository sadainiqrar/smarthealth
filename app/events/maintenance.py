"""Housekeeping for the event tables, run by Celery beat."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import TASKS, celery, task_settings
from app.core.clock import Clock
from app.events.models import OutboxEvent
from app.settings import Settings

#: Kafka keeps event topics this long too; a published row older than that has no use.
KEEP_PUBLISHED = timedelta(days=7)


async def purge_published(settings: Settings) -> int:
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.published_at.is_not(None),
                    OutboxEvent.published_at < Clock().now() - KEEP_PUBLISHED,
                )
            )
            return result.rowcount
    finally:
        await engine.dispose()


@celery.task(name="events.purge_published", acks_late=True)
def purge_published_outbox() -> int:
    """Daily: delete outbox rows published more than a week ago. Unpublished rows are never
    touched, however old -- they are the backlog."""
    return asyncio.run(purge_published(task_settings()))


TASKS.register("events.purge_published", purge_published_outbox)
