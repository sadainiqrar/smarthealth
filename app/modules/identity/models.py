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


#: VARCHAR + CHECK rather than a native Postgres enum.
#:
#: The reason usually given for this — that `ALTER TYPE ... ADD VALUE` cannot run
#: inside a transactional migration — has been **false since PostgreSQL 12**, and
#: this project runs PG16. The manual says only that a value added inside a
#: transaction block "cannot be used until after the transaction has been
#: committed". The stale belief is widespread because Alembic's own documentation
#: still states the old rule.
#:
#: The reasons that do hold:
#:  - Postgres has ADD VALUE and RENAME VALUE but **no DROP VALUE at all**, so
#:    removing a value means creating a new type, rewriting every dependent column,
#:    and dropping the old one. With a CHECK constraint it is one DDL statement and
#:    no table rewrite.
#:  - `alembic autogenerate` does not detect enum value changes, so a stale type
#:    produces a clean diff — the failure mode is silence.
#:  - A value added in a transaction cannot be used in that same transaction, so
#:    add-a-status-and-backfill needs two migrations either way.
#:
#: Cost of this choice: a varchar rather than the enum's 4 bytes, and no
#: database-level type identity shared across columns.
#:
#: Separately, `create_constraint=True` is required — a library default, not an
#: argument for the above. SQLAlchemy 2.0 defaults it to False, which would leave a
#: bare VARCHAR accepting any string.
role_column = Enum(
    UserRole,
    native_enum=False,
    create_constraint=True,
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
