"""Appointment request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.modules.scheduling.models import AppointmentStatus, VisitStatus, WaitlistStatus


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


class VisitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    appointment_id: uuid.UUID
    status: VisitStatus
    checked_in_at: datetime
    seen_at: datetime | None = None
    completed_at: datetime | None = None


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class RescheduleCreate(BaseModel):
    new_slot_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=2000)


class RescheduleAccepted(BaseModel):
    """202: the new appointment is pending and the old one still stands until the new one
    confirms. Poll GET /appointments/{appointment_id}."""

    appointment_id: uuid.UUID
    rescheduled_from: uuid.UUID


class WaitlistCreate(BaseModel):
    patient_id: uuid.UUID
    provider_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    # AwareDatetime: a window without an offset is ambiguous (which clinic's midnight?),
    # and comparing it with an aware slot time would raise -- a 500, not a 422.
    window_start: AwareDatetime
    window_end: AwareDatetime

    @model_validator(mode="after")
    def _window_moves_forward(self) -> WaitlistCreate:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class WaitlistRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    provider_id: uuid.UUID | None
    department_id: uuid.UUID | None
    window_start: datetime
    window_end: datetime
    status: WaitlistStatus
    created_at: datetime


class SlotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_id: uuid.UUID
    clinic_id: uuid.UUID
    department_id: uuid.UUID | None
    starts_at: datetime
    ends_at: datetime
