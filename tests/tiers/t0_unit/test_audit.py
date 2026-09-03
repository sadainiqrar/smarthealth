import enum
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.audit import AuditEvent, AuditLog
from app.core.clock import FixedClock

pytestmark = pytest.mark.unit

INSTANT = datetime(2026, 9, 3, 10, 30, tzinfo=UTC)


class _FakeCollection:
    """Records what would have been written, so the unit tier needs no Mongo."""

    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def insert_one(self, document: dict) -> None:
        self.documents.append(document)


async def test_record_writes_one_document_with_the_clock_timestamp():
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(
            entity_type="patient",
            entity_id="p1",
            action="registered",
            actor_user_id="u1",
            before=None,
            after={"mrn": "MRN-1"},
        )
    )

    assert len(collection.documents) == 1
    document = collection.documents[0]
    assert document["entity_type"] == "patient"
    assert document["entity_id"] == "p1"
    assert document["action"] == "registered"
    assert document["actor_user_id"] == "u1"
    assert document["before"] is None
    assert document["after"] == {"mrn": "MRN-1"}
    assert document["at"] == INSTANT


async def test_an_unattributed_change_is_allowed_but_explicit():
    """A CLI or background job has no acting user; the field is None, not missing."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(entity_type="user", entity_id="u9", action="created")
    )

    assert collection.documents[0]["actor_user_id"] is None
    assert "actor_user_id" in collection.documents[0]


async def test_before_and_after_default_to_none():
    event = AuditEvent(entity_type="patient", entity_id="p1", action="viewed")
    assert event.before is None
    assert event.after is None


def test_an_audit_event_is_immutable():
    """An event that could be mutated after construction is not a record of anything."""
    event = AuditEvent(entity_type="patient", entity_id="p1", action="registered")
    with pytest.raises(AttributeError):
        event.action = "tampered"


async def test_the_stored_timestamp_is_timezone_aware_utc():
    """A naive datetime in Mongo silently loses its offset, making the audit trail
    ambiguous about which timezone it was written in."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(AuditEvent(entity_type="patient", entity_id="p1", action="viewed"))

    stored_at = collection.documents[0]["at"]
    assert stored_at.tzinfo is not None
    assert stored_at.utcoffset() == UTC.utcoffset(None)


class _Status(enum.Enum):
    ACTIVE = "active"


async def test_a_bare_date_becomes_an_iso_string():
    """Verified against pymongo 4.17.0: `bson.encode({"d": date(2000, 1, 1)})` raises
    `InvalidDocument`. Because the audit write is awaited inside the request, that
    would fail the whole mutation, not just the audit entry."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(
            entity_type="patient",
            entity_id="p1",
            action="registered",
            after={"date_of_birth": date(1990, 4, 17)},
        )
    )

    assert collection.documents[0]["after"] == {"date_of_birth": "1990-04-17"}


async def test_a_real_datetime_is_preserved_as_a_datetime():
    """The subclass trap: `datetime` *is* a `date`, so an `isinstance(value, date)`
    check placed first would stringify every genuine timestamp and silently destroy
    the audit trail's queryability on time ranges."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))
    seen_at = datetime(2026, 9, 3, 9, 15, tzinfo=UTC)

    await log.record(
        AuditEvent(
            entity_type="visit", entity_id="v1", action="checked_in", after={"seen_at": seen_at}
        )
    )

    stored = collection.documents[0]["after"]["seen_at"]
    assert isinstance(stored, datetime)
    assert stored == seen_at


async def test_a_decimal_becomes_a_string():
    """BSON has no arbitrary-precision decimal that round-trips a Python `Decimal`,
    and a float would quietly lose cents off a billing amount."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(
            entity_type="invoice",
            entity_id="i1",
            action="issued",
            after={"total": Decimal("12.50")},
        )
    )

    assert collection.documents[0]["after"] == {"total": "12.50"}


async def test_an_enum_becomes_its_value():
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(
            entity_type="user", entity_id="u1", action="updated", after={"status": _Status.ACTIVE}
        )
    )

    assert collection.documents[0]["after"] == {"status": "active"}


async def test_normalisation_recurses_into_nested_dicts_and_lists():
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(
            entity_type="patient",
            entity_id="p1",
            action="updated",
            before={"profile": {"date_of_birth": date(1990, 4, 17)}},
            after={"appointments": [{"day": date(2026, 9, 4)}, date(2026, 9, 5)]},
        )
    )

    document = collection.documents[0]
    assert document["before"] == {"profile": {"date_of_birth": "1990-04-17"}}
    assert document["after"] == {"appointments": [{"day": "2026-09-04"}, "2026-09-05"]}


async def test_normalisation_leaves_none_payloads_as_none():
    """`None` means "there was no before/after state" and must not become `{}`."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(AuditEvent(entity_type="patient", entity_id="p1", action="viewed"))

    assert collection.documents[0]["before"] is None
    assert collection.documents[0]["after"] is None


async def test_a_uuid_is_left_alone_for_pymongo_to_encode():
    """`app.db.mongo` sets `uuidRepresentation="standard"`, so a UUID round-trips as a
    UUID. Stringifying it here would lose that."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))
    identifier = uuid.uuid4()

    await log.record(
        AuditEvent(entity_type="patient", entity_id="p1", action="viewed", after={"id": identifier})
    )

    assert collection.documents[0]["after"]["id"] is identifier


async def test_an_unknown_unencodable_type_is_left_alone_to_fail_loudly():
    """Normalisation converts the known-unencodable types and nothing else. Blanket
    `str()` would push an unreviewed repr into a record meant to be evidence."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))
    sentinel = object()

    await log.record(
        AuditEvent(entity_type="patient", entity_id="p1", action="viewed", after={"x": sentinel})
    )

    assert collection.documents[0]["after"]["x"] is sentinel


def test_importing_audit_does_not_load_fastapi_or_starlette():
    """Must run in a fresh interpreter: a pytest process that already imported FastAPI
    for another test would make this pass regardless of what `app.core.audit` itself
    imports, defeating the point of the check.

    A Temporal activity, a Celery task, or any plain script must be able to construct
    an `AuditLog` without pulling in the ASGI stack — that is why `get_audit_log` lives
    in `app.api.deps`, not here.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.core.audit; "
            "print('fastapi' in sys.modules, 'starlette' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False"
