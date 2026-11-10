"""Sending a message a person wrote or approved (Weeks 3-5 spec section 9.5).

`POST /notifications` is how an approved draft -- the assistant drafts, staff edit and
approve -- reaches a patient. Delivery, throttling, retries and the dead-letter queue stay
in this module (the same `queue` and Celery send as every automatic message), so a message
sent from the assistant is indistinguishable from any other once it is queued, and Part B
never holds an SMTP client.

Who may send: front desk and admin to any patient; a provider only to a patient they treat
(the care relationship, as for the clinical record -- R-14), with 404 otherwise so the
answer does not reveal whether the patient exists. Every message is audited with its text.
An `Idempotency-Key` is required: a retried approval queues the message once.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.providers.public as providers
import app.modules.scheduling.public as scheduling
from app.api.deps import get_audit_log, get_session
from app.core.audit import AuditEvent, AuditLog
from app.core.errors import NotFound
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.notifications import public, service

router = APIRouter(prefix="/notifications", tags=["notifications"])

SENDERS = (UserRole.FRONT_DESK, UserRole.PROVIDER, UserRole.ADMIN)


class MessageIn(BaseModel):
    patient_id: uuid.UUID
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    appointment_id: uuid.UUID | None = None


class MessageQueued(BaseModel):
    notification_id: uuid.UUID | None
    #: queued | duplicate (this key was already sent) | unreachable (no email or phone)
    status: str


def get_sender(request: Request):
    """Enqueues the Celery send for a notification id. Overridden in the contract tier."""
    return request.app.state.enqueue_send


@router.post("", response_model=MessageQueued, status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    body: MessageIn,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    enqueue=Depends(get_sender),
    claims: TokenClaims = Depends(require_role(*SENDERS)),
) -> MessageQueued:
    """Queue a message to one patient: the patient's email if they have one, else SMS."""
    if claims.role is UserRole.PROVIDER:
        provider_id = await providers.provider_id_for_user(session, uuid.UUID(claims.subject))
        if provider_id is None or not await scheduling.has_care_relationship(
                session, provider_id, body.patient_id):
            raise NotFound(f"patient {body.patient_id} does not exist")
    recipient = await public.recipient_for(session, body.patient_id)
    if not recipient.name and not (recipient.email or recipient.phone):
        raise NotFound(f"patient {body.patient_id} does not exist")
    key = f"staff:{claims.subject}:{idempotency_key}"
    notification_id = await service.queue(
        session, key=key, kind="staff_message", recipient=recipient,
        appointment_id=body.appointment_id,
        context={"subject": body.subject, "body": body.body}, enqueue=enqueue,
    )
    if notification_id is None and not (recipient.email or recipient.phone):
        outcome = "unreachable"
    elif notification_id is None:
        outcome = "duplicate"
    else:
        outcome = "queued"
    await session.commit()  # the send is enqueued after this commit, never before
    if notification_id is not None:
        # After the commit: the trail never records a message that rolled back.
        await audit.record(AuditEvent(
            entity_type="notification", entity_id=str(notification_id), action="staff_sent",
            actor_user_id=claims.subject, before=None,
            after={"patient_id": str(body.patient_id), "subject": body.subject,
                   "body": body.body, "idempotency_key": idempotency_key}))
    return MessageQueued(notification_id=notification_id, status=outcome)
