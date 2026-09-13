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
#: The reason that does hold: Postgres has ADD VALUE and RENAME VALUE but **no
#: DROP VALUE at all**. Removing a value means creating a new type, an
#: `ALTER COLUMN ... TYPE ... USING` per dependent column with a table rewrite, then
#: dropping the old type — and the reverse again for the downgrade. The CHECK
#: equivalent is a drop-and-recreate in one transactional migration, no rewrite.
#:
#: NOT an argument, though it reads like one: "alembic autogenerate misses enum
#: value changes". It misses CHECK expression changes too — detection of named
#: CHECK constraints is off by default, matches on name only, and per Alembic's
#: docs "expression changes are never detected". Both choices give a clean diff
#: over a stale database, so autogenerate is symmetric here. What differs is the
#: remediation cost once you notice, which is the DROP VALUE point above.
#:
#: Also minor: a value added inside a transaction cannot be used until that
#: transaction commits, so add-a-status-and-backfill is two migrations either way.
#:
#: Costs of this choice, priced rather than waved off:
#:  - ~5 bytes/row versus the enum's 4-byte OID. At the tens of thousands of
#:    appointments this system is specified for, under a megabyte. Irrelevant here;
#:    it would flip at tens of millions of rows with no expected churn.
#:  - No type reuse across columns.
#:  - **Loss of database-level type identity** — the real cost. Postgres would stop
#:    you comparing an appointment status to a visit status; as varchar, both are
#:    text and it shrugs. The usual mitigation, disjoint value sets, does NOT hold
#:    here: 'completed' is in both AppointmentStatus and VisitStatus, and
#:    'cancelled' is in both AppointmentStatus and WaitlistStatus. SQLAlchemy's Enum
#:    in Python is the only remaining guard.
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
