"""Appointment request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.scheduling.models import AppointmentStatus


class BookingCreate(BaseModel):
    patient_id: uuid.UUID
    slot_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=2000)


class BookingAccepted(BaseModel):
    """202: the booking is pending. Poll GET /appointments/{appointment_id}."""

    appointment_id: uuid.UUID
    status: AppointmentStatus = AppointmentStatus.PENDING


class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: AppointmentStatus
    #: True only between the 202 and the workflow's first activity writing the row.
    in_progress: bool = False
    patient_id: uuid.UUID | None = None
    provider_id: uuid.UUID | None = None
    clinic_id: uuid.UUID | None = None
    slot_id: uuid.UUID | None = None
    reason: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime | None = None


class SlotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_id: uuid.UUID
    clinic_id: uuid.UUID
    department_id: uuid.UUID | None
    starts_at: datetime
    ends_at: datetime
