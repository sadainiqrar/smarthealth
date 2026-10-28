"""Clinics, departments, providers, and their bookable slots.

Departments are scoped to a clinic, and providers relate to departments many-to-many
so a clinician covering two sites stays bookable at both (design spec 3.5).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


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

    __table_args__ = (
        UniqueConstraint("clinic_id", "name", name="uq_departments_clinic_id_name"),
        # Composite target for the department/clinic coherence FKs.
        UniqueConstraint("id", "clinic_id", name="uq_departments_id_clinic"),
    )


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
