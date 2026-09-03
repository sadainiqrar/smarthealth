import subprocess
import sys
from datetime import UTC, datetime

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
