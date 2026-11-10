"""The notifications consumer: domain events that a patient should hear about.

One consumer, `notifications.patient-messages`, over the events that change what a patient
can expect. Each event queues at most one message, keyed by the event id, so a redelivery
(the runtime dedupes too) cannot queue a second; the message is sent by Celery once the
consumer's transaction commits. Kafka says what happened, this decides who is told, Celery
sends it -- the three-way split the stack exists for (PRD D-7).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import enqueue_send
from app.events.consumer import ConsumerContext, ConsumerSpec, Event
from app.modules.notifications import service
from app.modules.notifications.public import recipient_for

#: topic -> (message kind, payload field holding the appointment, field holding the time).
MESSAGES: dict[str, tuple[str, str | None, str | None]] = {
    "appointments.booked": ("booking_confirmed", "appointment_id", "starts_at"),
    "appointments.cancelled": ("appointment_cancelled", "appointment_id", "starts_at"),
    "appointments.rescheduled": ("appointment_rescheduled", "new_appointment_id", "new_starts_at"),
    "appointments.no_show": ("appointment_missed", "appointment_id", "starts_at"),
    "billing.status_updated": ("refund_issued", "appointment_id", None),
    "clinical.note_signed": ("visit_summary_ready", "appointment_id", None),
    "clinical.test_ordered": ("test_ordered", None, None),
}


async def on_patient_event(session: AsyncSession, event: Event, context: ConsumerContext) -> None:
    payload = event.payload
    if event.topic == "billing.status_updated" and payload["billing_status"] != "refunded":
        return  # only a refund is news to the patient; holds and captures are not
    kind, appointment_field, time_field = MESSAGES[event.topic]
    message_context: dict[str, Any] = {
        "when": payload.get(time_field) if time_field else None,
        "reason": payload.get("reason"),
    }
    patient_id = uuid.UUID(payload["patient_id"])
    await service.queue(
        session, key=f"{event.topic}:{event.event_id}", kind=kind,
        recipient=await recipient_for(session, patient_id),
        appointment_id=uuid.UUID(payload[appointment_field]) if appointment_field else None,
        context=message_context,
        enqueue=enqueue_send,
    )


NOTIFICATION_CONSUMERS: tuple[ConsumerSpec, ...] = (
    ConsumerSpec(
        name="notifications.patient-messages", topics=tuple(MESSAGES),
        handler=on_patient_event, effect="database",
    ),
)
