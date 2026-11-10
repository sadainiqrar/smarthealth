"""What other modules may use from notifications (the D-15 pattern).

Scheduling's visit workflow asks for an appointment's reminder here when its timer fires
(spec D-26: Temporal owns the timing, Celery only delivers). A declared edge in
`CROSS_MODULE_ALLOWED`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.patients.public as patients
from app.modules.notifications import service


async def recipient_for(session: AsyncSession, patient_id: uuid.UUID) -> service.Recipient:
    contact = await patients.contact_for(session, patient_id)
    if contact is None:
        return service.Recipient(patient_id, "", None, None)
    return service.Recipient(patient_id, contact.first_name, contact.email, contact.phone)


async def queue_reminder(
    session: AsyncSession, *, appointment_id: uuid.UUID, patient_id: uuid.UUID,
    starts_at: datetime, enqueue: Callable[[str], None],
) -> uuid.UUID | None:
    """Queue the appointment's reminder, once (keyed by the appointment)."""
    return await service.queue(
        session, key=f"reminder:{appointment_id}", kind="appointment_reminder",
        recipient=await recipient_for(session, patient_id), appointment_id=appointment_id,
        context={"when": starts_at}, enqueue=enqueue,
    )
