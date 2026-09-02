"""Authentication identity.

A `User` is a login, not a person. Patients and providers are separate records that
may or may not have one — front-desk staff register walk-in patients who have no
credentials at all (design spec section 3.1).
"""

from __future__ import annotations

import enum

from sqlalchemy import Boolean, Enum, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserRole(str, enum.Enum):
    PATIENT = "patient"
    PROVIDER = "provider"
    FRONT_DESK = "front_desk"
    ADMIN = "admin"


#: VARCHAR + CHECK rather than a native Postgres enum: adding a value to a native
#: enum requires ALTER TYPE, which cannot run inside a transactional migration.
role_column = Enum(
    UserRole,
    native_enum=False,
    length=20,
    values_callable=lambda enum_class: [member.value for member in enum_class],
)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(role_column)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
