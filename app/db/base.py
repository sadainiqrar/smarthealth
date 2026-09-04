"""Declarative base, shared metadata, and column mixins.

The naming convention is load-bearing: without it Postgres generates constraint
names, which makes Alembic diffs unstable and hand-written downgrades impossible.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: `updated_at` below carries `onupdate=func.now()`, a SQL expression, so after an
    #: UPDATE SQLAlchemy cannot know the resulting value and expires the attribute.
    #: Reading it afterwards -- serialising a PATCH response -- would emit a lazy SELECT
    #: outside a greenlet context and raise `MissingGreenlet`, which made every PATCH
    #: endpoint return 500. `eager_defaults` makes SQLAlchemy append RETURNING to the
    #: UPDATE it already issues, so the value arrives with the statement: no second
    #: round trip, and no per-service `session.refresh()` for each new model to
    #: remember. Set on `Base` rather than on `TimestampMixin` so it also covers a
    #: future model with a server default that does not use the mixin. Verified with
    #: `echo`: the emitted SQL is `UPDATE ... RETURNING updated_at` and the follow-up
    #: SELECT is gone.
    __mapper_args__ = {"eager_defaults": True}


class UUIDPrimaryKeyMixin:
    """UUID primary keys: stable inside event payloads, no sequence contention."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
