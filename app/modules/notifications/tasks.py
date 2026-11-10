"""Celery tasks for notifications: send one message, sweep stuck ones.

Tasks are synchronous functions; the domain code is async. Each task runs one
`asyncio.run` over a short-lived engine (`NullPool`, so no connection outlives the loop
that opened it) -- at a notification's rate the connect cost is noise, and nothing is
shared between tasks that a crash could leave half-used.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import timedelta

from celery.exceptions import Reject
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import TASKS, celery, enqueue_send, task_settings
from app.core.clock import Clock
from app.core.ratelimit import RedisRateLimiter
from app.db.redis import create_redis_client
from app.modules.notifications import service
from app.modules.notifications.channels import SENDERS, ChannelError
from app.settings import Settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
#: How long a queued row may wait for its enqueue before the sweep re-sends it.
STUCK_AFTER = timedelta(minutes=1)


@dataclass(frozen=True)
class SendOutcome:
    status: str  # "sent", "duplicate", "throttled", "retry", "failed"
    retry_in: int | None = None


def _sessions(settings: Settings):
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def send(settings: Settings, notification_id: uuid.UUID, *, retries: int) -> SendOutcome:
    """One attempt at one message. The decisions; the task wrapper only acts on them."""
    clock = Clock()
    engine, sessions = _sessions(settings)
    redis = create_redis_client(settings)
    try:
        async with sessions() as session:
            row = await service.claim(session, notification_id, clock.now())
            await session.commit()
        if row is None:
            return SendOutcome("duplicate")  # sent already, or another worker has it

        limiter = RedisRateLimiter(redis, settings)
        decision = await limiter.hit(
            f"notify:{row.patient_id}", limit=settings.notification_hourly_limit,
            window=timedelta(hours=1),
        )
        if not decision.allowed:
            async with sessions() as session:
                await service.release(session, notification_id, error="throttled", final=False)
                await session.commit()
            return SendOutcome("throttled", retry_in=decision.retry_after)

        try:
            message_id = await asyncio.to_thread(
                SENDERS[row.channel.value], settings, to=row.recipient, subject=row.subject,
                body=row.body, key=row.idempotency_key,
            )
        except ChannelError as exc:
            final = retries >= MAX_RETRIES
            async with sessions() as session:
                await service.release(session, notification_id, error=str(exc), final=final)
                await session.commit()
            if final:
                return SendOutcome("failed")
            # Exponential backoff with jitter: a mail server that is down must not be hit
            # by every queued message at the same instant when it comes back.
            return SendOutcome("retry", retry_in=int(5 * 2 ** retries + random.uniform(0, 5)))

        async with sessions() as session:
            await service.mark_sent(session, notification_id, message_id=message_id,
                                    now=clock.now())
            await session.commit()
        return SendOutcome("sent")
    finally:
        await redis.aclose()
        await engine.dispose()


@celery.task(name="notifications.send", bind=True, acks_late=True, max_retries=None)
def send_notification(self, notification_id: str) -> str:
    outcome = asyncio.run(
        send(task_settings(), uuid.UUID(notification_id), retries=self.request.retries)
    )
    if outcome.status in ("retry", "throttled"):
        # A throttled message is not a failure: it waits for the patient's window to reset
        # and does not use up a retry (throttling re-sends without counting).
        raise self.retry(countdown=outcome.retry_in, max_retries=None)
    if outcome.status == "failed":
        logger.error("notification failed for good; dead-lettered",
                     extra={"notification_id": notification_id})
        # requeue=False with the queue's dead-letter arguments: RabbitMQ moves the message
        # to notifications.dlq, kept for inspection, instead of dropping it.
        raise Reject(f"notification {notification_id} undeliverable", requeue=False)
    return outcome.status


async def requeue_stuck(settings: Settings) -> list[str]:
    engine, sessions = _sessions(settings)
    try:
        async with sessions() as session:
            ids = await service.stuck(session, Clock().now(), older_than=STUCK_AFTER)
    finally:
        await engine.dispose()
    for notification_id in ids:
        enqueue_send(str(notification_id))
    return [str(notification_id) for notification_id in ids]


@celery.task(name="notifications.requeue_stuck", acks_late=True)
def requeue_stuck_notifications() -> int:
    """Every minute: queued rows whose enqueue was lost (the process died between commit
    and enqueue), and claims whose worker died. `claim` keeps a double send impossible."""
    ids = asyncio.run(requeue_stuck(task_settings()))
    if ids:
        logger.info("requeued stuck notifications", extra={"count": len(ids)})
    return len(ids)


TASKS.register("notifications.send", send_notification)
TASKS.register("notifications.requeue_stuck", requeue_stuck_notifications)
