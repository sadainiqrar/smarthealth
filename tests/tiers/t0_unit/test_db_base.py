import uuid

import pytest
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

pytestmark = pytest.mark.unit


class _Parent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "_probe_parents"
    name: Mapped[str] = mapped_column(String(50))


class _Child(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "_probe_children"
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("_probe_parents.id"))
    code: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint("code"),)


def test_primary_key_is_a_uuid_with_a_default():
    column = _Parent.__table__.c.id
    assert column.primary_key
    assert column.default is not None


def test_timestamps_are_timezone_aware_and_not_nullable():
    """A naive timestamp cannot be compared correctly across clinics in other zones."""
    for name in ("created_at", "updated_at"):
        column = _Parent.__table__.c[name]
        assert column.type.timezone is True
        assert not column.nullable


def test_constraint_names_follow_the_naming_convention():
    """Unnamed constraints get database-generated names, which makes migrations
    undiffable and drops impossible to write by hand."""
    assert _Parent.__table__.primary_key.name == "pk__probe_parents"
    foreign_key = next(iter(_Child.__table__.foreign_key_constraints))
    assert foreign_key.name == "fk__probe_children_parent_id__probe_parents"
    unique = next(
        c for c in _Child.__table__.constraints if isinstance(c, UniqueConstraint)
    )
    assert unique.name == "uq__probe_children_code"


def test_every_registered_table_uses_the_shared_metadata():
    assert _Parent.__table__.metadata is Base.metadata
    assert _Child.__table__.metadata is Base.metadata
