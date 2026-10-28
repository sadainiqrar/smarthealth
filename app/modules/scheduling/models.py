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
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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


class SlotStatus(str, enum.Enum):
    FREE = "free"
    HELD = "held"
    BOOKED = "booked"
    BLOCKED = "blocked"


#: VARCHAR + CHECK, not a native enum. `create_constraint=True` is required —
#: SQLAlchemy 2.0 defaults it to False, leaving a bare VARCHAR that accepts anything.
slot_status_column = Enum(
    SlotStatus,
    native_enum=False,
    create_constraint=True,
    length=16,
    values_callable=lambda enum_class: [member.value for member in enum_class],
)


class ProviderSlot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One bookable interval.

    Lives in scheduling, not providers (D-9): scheduling is the only module that writes it.

    Booking is an atomic conditional update — `SET status='held' WHERE id=:id AND
    status='free'` — so zero rows affected *is* the conflict signal, enforced by the
    database rather than by application logic (design spec 3.2).

    A GiST exclusion constraint preventing overlapping slots for one provider is
    added in the baseline migration; SQLAlchemy cannot express it portably, and it
    needs the `btree_gist` extension.
    """

    __tablename__ = "provider_slots"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE")
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlotStatus] = mapped_column(
        slot_status_column, server_default=text("'free'")
    )
    #: Optimistic-concurrency counter for the Week 2 booking workflow.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="end_after_start"),
        Index("ix_provider_slots_provider_starts", "provider_id", "starts_at"),
        Index("ix_provider_slots_status_starts", "status", "starts_at"),
        # Composite target for appointments' coherence FK: lets an appointment prove
        # its slot really belongs to the provider and clinic it claims.
        UniqueConstraint(
            "id", "provider_id", "clinic_id", name="uq_provider_slots_id_provider_clinic"
        ),
        # A slot's department must belong to the slot's own clinic.
        ForeignKeyConstraint(
            ["department_id", "clinic_id"],
            ["departments.id", "departments.clinic_id"],
            name="fk_provider_slots_department_clinic",
        ),
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
        # The slot must belong to the provider and clinic this appointment names.
        # MATCH SIMPLE means this is skipped while slot_id is NULL (pre-claim) and
        # enforced the moment a slot is attached.
        ForeignKeyConstraint(
            ["slot_id", "provider_id", "clinic_id"],
            [
                "provider_slots.id",
                "provider_slots.provider_id",
                "provider_slots.clinic_id",
            ],
            name="fk_appointments_slot_provider_clinic",
        ),
        # The department must belong to the clinic this appointment names.
        ForeignKeyConstraint(
            ["department_id", "clinic_id"],
            ["departments.id", "departments.clinic_id"],
            name="fk_appointments_department_clinic",
        ),
        # A confirmed appointment without a claimed slot is a contradiction: confirmed
        # is only reachable after slot reservation succeeds.
        CheckConstraint(
            "status <> 'confirmed' OR slot_id IS NOT NULL",
            name="confirmed_requires_slot",
        ),
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
