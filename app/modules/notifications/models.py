"""One row per message to a patient: what to send, where, and what happened to it.

The row is written in the same transaction as the reason for it (a consumer's
`processed_events` mark, an activity's commit), and the Celery task that sends it is
enqueued only after that commits. `idempotency_key` -- the triggering event and the channel
-- is unique, so a second trigger for the same reason cannot queue a second message, and
the task's first step claims the row, so a second delivery of the task cannot send twice.

    queued --claim--> sending --> sent
                         \\--> failed   (retries exhausted; also dead-lettered in RabbitMQ)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotificationStatus(str, enum.Enum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class Channel(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"


def _enum(enum_class: type[enum.Enum], length: int) -> Enum:
    return Enum(
        enum_class, native_enum=False, create_constraint=True, length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "notifications"

    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    channel: Mapped[Channel] = mapped_column(_enum(Channel, 8))
    #: An email address or a phone number, resolved when the message was queued.
    recipient: Mapped[str] = mapped_column(String(320))
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, 16), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
