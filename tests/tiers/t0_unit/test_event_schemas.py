import uuid
from datetime import UTC, datetime

import pytest

from app.events import schemas
from app.events.outbox import emit

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def _cancelled(**overrides):
    payload = {
        "appointment_id": str(uuid.uuid4()), "patient_id": str(uuid.uuid4()),
        "provider_id": str(uuid.uuid4()), "slot_id": None, "starts_at": NOW,
        "reason": "moving away", "initiated_by": None,
    }
    payload.update(overrides)
    return payload


class _Session:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


def test_emit_adds_a_validated_row_with_json_payload():
    session = _Session()
    row = emit(session, "appointments.cancelled", key="a1", payload=_cancelled())
    assert session.added == [row]
    assert row.topic == "appointments.cancelled" and row.key == "a1"
    assert row.payload["starts_at"] == NOW.isoformat()
    assert row.schema_version == 1 and row.producer == "part-a"


def test_emit_refuses_a_payload_missing_a_required_field():
    payload = _cancelled()
    del payload["reason"]
    with pytest.raises(schemas.InvalidEvent):
        emit(_Session(), "appointments.cancelled", key="a1", payload=payload)


def test_emit_refuses_a_naive_timestamp():
    with pytest.raises(schemas.InvalidEvent):
        emit(_Session(), "appointments.cancelled", key="a1",
             payload=_cancelled(starts_at=datetime(2026, 11, 10, 9, 0)))


def test_emit_refuses_an_unknown_topic():
    with pytest.raises(KeyError):
        emit(_Session(), "appointments.teleported", key="a1", payload={})


def test_json_round_trips_to_avro_values():
    stored = schemas.to_json(_cancelled())
    record = schemas.from_json(schemas.parsed_schema("appointments.cancelled"), {
        "event_id": str(uuid.uuid4()), "event_type": "appointments.cancelled",
        "schema_version": 1, "occurred_at": NOW.isoformat(), "producer": "part-a",
        "subject_id": "a1", "payload": stored,
    })
    assert record["occurred_at"] == NOW
    assert record["payload"]["starts_at"] == NOW
    assert record["payload"]["slot_id"] is None
