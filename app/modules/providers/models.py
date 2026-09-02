"""Clinics, departments, providers, and their bookable slots.

Departments are scoped to a clinic, and providers relate to departments many-to-many
so a clinician covering two sites stays bookable at both (design spec 3.5).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SlotStatus(str, enum.Enum):
    FREE = "free"
    HELD = "held"
    BOOKED = "booked"
    BLOCKED = "blocked"


slot_status_column = Enum(
    SlotStatus,
    native_enum=False,
    length=16,
    values_callable=lambda enum_class: [member.value for member in enum_class],
)


class Clinic(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "clinics"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    #: IANA zone. Slot generation is wrong without it once clinics span zones.
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class Department(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "departments"

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))

    __table_args__ = (UniqueConstraint("clinic_id", "name"),)


class Provider(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "providers"

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    specialty: Mapped[str] = mapped_column(String(120), index=True)
    license_number: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )


provider_departments = Table(
    "provider_departments",
    Base.metadata,
    Column(
        "provider_id",
        PGUUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "department_id",
        PGUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ProviderSlot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One bookable interval.

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
    )
