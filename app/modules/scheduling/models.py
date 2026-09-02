"""Appointments, visits, and the waitlist.

An appointment is the booking; a visit is what actually happened. Keeping them
separate means Average Wait Time falls out of `seen_at - checked_in_at`, a no-show is
simply an appointment with no visit, and two different workflows stop mutating one
row (design spec 3.3).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AppointmentStatus(str, enum.Enum):
    #: Created, workflow not yet finished. The only legal initial state.
    PENDING = "pending"
    #: Reachable only after slot reservation, billing pre-check and notification
    #: scheduling have all succeeded.
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    #: Superseded by a newer appointment that points back via rescheduled_from_id.
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    #: The booking workflow failed and its compensation ran.
    FAILED = "failed"


class VisitStatus(str, enum.Enum):
    CHECKED_IN = "checked_in"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class WaitlistStatus(str, enum.Enum):
    ACTIVE = "active"
    FULFILLED = "fulfilled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def _enum_column(enum_class: type[enum.Enum], length: int) -> Enum:
    """VARCHAR + CHECK, not a native enum.

    `create_constraint=True` is required: SQLAlchemy 2.0 defaults it to False, which
    emits a bare VARCHAR that accepts any string - the exact validation this choice
    exists to preserve.
    """
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Appointment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "appointments"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT"), index=True
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="RESTRICT")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: The slot this booking claims. A partial unique index in the baseline migration
    #: guarantees at most one *live* appointment per slot, independently of whether
    #: the application's conditional update is correct.
    slot_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("provider_slots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum_column(AppointmentStatus, 16), server_default=text("'pending'"), index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"),
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_appointments_patient_status", "patient_id", "status"),
    )


class Visit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "visits"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"),
        unique=True,
    )
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Average Wait Time = avg(seen_at - checked_in_at).
    seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[VisitStatus] = mapped_column(
        _enum_column(VisitStatus, 16), server_default=text("'checked_in'")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class WaitlistEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Who to promote when a slot is released (Week 2's cancellation flow)."""

    __tablename__ = "waitlist_entries"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=True,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=True,
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[WaitlistStatus] = mapped_column(
        _enum_column(WaitlistStatus, 16), server_default=text("'active'"), index=True
    )
