"""Delivery channels: email over SMTP, and a logging SMS fake.

SMTP goes to Mailpit in compose, which keeps every message in a web inbox
(http://localhost:8025), so a demo shows a real message arrive. SMS has no provider in this
project (spec O-8): the fake logs the message and returns an id, which keeps the channel's
shape -- send, get an id, record it -- without an account.
"""

from __future__ import annotations

import logging
import smtplib
import uuid
from email.message import EmailMessage

from app.settings import Settings

logger = logging.getLogger(__name__)


class ChannelError(Exception):
    """The channel could not deliver now. Retryable."""


def send_email(settings: Settings, *, to: str, subject: str, body: str, key: str) -> str:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    # The idempotency key as Message-ID: a resend after a lost acknowledgement is the same
    # message to any server that deduplicates on it.
    message_id = f"<{key}@smarthealth.medinova.example>"
    message["Message-ID"] = message_id
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ChannelError(f"smtp: {exc!r}") from exc
    return message_id


def send_sms(settings: Settings, *, to: str, subject: str, body: str, key: str) -> str:
    message_id = f"sms-{uuid.uuid5(uuid.NAMESPACE_URL, key)}"
    logger.info("sms sent (fake channel)", extra={"to": to, "message_id": message_id})
    return message_id


SENDERS = {"email": send_email, "sms": send_sms}
