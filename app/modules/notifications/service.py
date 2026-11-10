"""Queueing, claiming and recording notifications.

`queue` is called inside someone else's transaction -- a Kafka consumer's (with its
`processed_events` mark) or an activity's -- and registers the Celery enqueue to run only
*after* that transaction commits. Enqueueing first would let a worker look for a row that
is not there yet, or that a rollback removed. If the process dies between the commit and
the enqueue, the beat sweep `requeue_stuck` finds the `queued` row and enqueues it again.

`claim` is the send task's first step and the reason a twice-delivered task sends once:
only one transaction can move a row from `queued` to `sending`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import event, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Channel, Notification, NotificationStatus
from app.modules.notifications.templates import render

logger = logging.getLogger(__name__)

#: A claimed row whose worker died is claimable again after this long.
CLAIM_TIMEOUT = timedelta(minutes=5)


def _after_commit(session: AsyncSession, callback: Callable[[], None]) -> None:
    """Run `callback` once the session's current transaction commits -- never on rollback."""

    def fire(_session: Any) -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - the sweep re-enqueues; the commit already happened
            logger.warning("enqueue after commit failed; the sweep will retry", exc_info=True)

    event.listen(session.sync_session, "after_commit", fire, once=True)


@dataclass(frozen=True)
class Recipient:
    """Who a message is for, as the caller resolved it (through `patients.public`: this
    service crosses no module boundary)."""

    patient_id: uuid.UUID
    name: str
    email: str | None
    phone: str | None


async def queue(
    session: AsyncSession, *, key: str, kind: str, recipient: Recipient,
    appointment_id: uuid.UUID | None, context: dict[str, Any],
    enqueue: Callable[[str], None],
) -> uuid.UUID | None:
    """Queue one message, once per `key`. Email if the patient has an address, else SMS.
    Returns its id, or None when it was already queued (a repeat) or the patient has no
    way to be reached."""
    if not (recipient.email or recipient.phone):
        logger.info("patient has no contact details; nothing queued",
                    extra={"patient_id": str(recipient.patient_id), "kind": kind})
        return None
    channel, address = (
        (Channel.EMAIL, recipient.email) if recipient.email else (Channel.SMS, recipient.phone)
    )
    subject, body = render(kind, {"name": recipient.name, **context})
    notification_id = await session.scalar(
        insert(Notification)
        .values(
            id=uuid.uuid4(), idempotency_key=f"{key}:{channel.value}", kind=kind,
            channel=channel, recipient=address, patient_id=recipient.patient_id,
            appointment_id=appointment_id, subject=subject, body=body,
            status=NotificationStatus.QUEUED, attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[Notification.idempotency_key])
        .returning(Notification.id)
    )
    if notification_id is None:
        return None
    _after_commit(session, lambda: enqueue(str(notification_id)))
    return notification_id


async def claim(
    session: AsyncSession, notification_id: uuid.UUID, now: datetime
) -> Notification | None:
    """Take the row for sending, or None if someone else has it or it is done."""
    return await session.scalar(
        update(Notification)
        .where(
            Notification.id == notification_id,
            or_(
                Notification.status == NotificationStatus.QUEUED,
                (Notification.status == NotificationStatus.SENDING)
                & (Notification.claimed_at < now - CLAIM_TIMEOUT),
            ),
        )
        .values(status=NotificationStatus.SENDING, claimed_at=now,
                attempts=Notification.attempts + 1)
        .returning(Notification)
    )


async def mark_sent(
    session: AsyncSession, notification_id: uuid.UUID, *, message_id: str, now: datetime
) -> None:
    await session.execute(
        update(Notification).where(Notification.id == notification_id)
        .values(status=NotificationStatus.SENT, sent_at=now, provider_message_id=message_id,
                last_error=None)
    )


async def release(
    session: AsyncSession, notification_id: uuid.UUID, *, error: str, final: bool
) -> None:
    """A failed attempt: back to `queued` for the retry, or `failed` for good."""
    await session.execute(
        update(Notification).where(Notification.id == notification_id)
        .values(status=NotificationStatus.FAILED if final else NotificationStatus.QUEUED,
                last_error=error[:2000], claimed_at=None)
    )


async def stuck(session: AsyncSession, now: datetime, *, older_than: timedelta) -> list[uuid.UUID]:
    """Rows queued longer than `older_than` (their enqueue was lost) or claimed by a worker
    that never finished. The beat sweep enqueues them again; `claim` keeps it safe."""
    rows = await session.scalars(
        select(Notification.id).where(
            or_(
                (Notification.status == NotificationStatus.QUEUED)
                & (Notification.updated_at < now - older_than),
                (Notification.status == NotificationStatus.SENDING)
                & (Notification.claimed_at < now - CLAIM_TIMEOUT),
            )
        ).limit(500)
    )
    return list(rows)
