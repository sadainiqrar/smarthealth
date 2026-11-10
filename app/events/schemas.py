"""The Avro schemas in contracts/events/: loaded, validated against, converted to.

The outbox stores an event's payload as JSON -- readable in psql, and writable without a
network call inside the business transaction. It is *validated* against the topic's Avro
schema when it is written, so a malformed event fails the change that produced it, not
the relay minutes later where nobody can fix it. The relay converts the JSON back into
the Python values Avro serialises (`from_json`).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import fastavro
from fastavro.validation import ValidationError, validate

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "events"


class InvalidEvent(ValueError):
    """An event does not match its topic's schema."""


@lru_cache
def schema_text(name: str) -> str:
    path = CONTRACTS / f"{name}.avsc"
    if not path.exists():
        raise KeyError(f"no schema {path}")
    return path.read_text(encoding="utf-8")


@lru_cache
def parsed_schema(name: str) -> Any:
    return fastavro.parse_schema(json.loads(schema_text(name)))


def schema_names() -> set[str]:
    return {path.name.removesuffix(".avsc") for path in CONTRACTS.glob("*.avsc")}


def to_json(value: Any) -> Any:
    """Python values to JSON-safe ones: datetimes as ISO-8601 UTC, uuids as strings."""
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InvalidEvent("event timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def from_json(schema: Any, value: Any) -> Any:
    """JSON values back to what Avro serialises, guided by the parsed schema."""
    if isinstance(schema, list):  # a union; only ["null", X] is used
        if value is None:
            return None
        branch = next(item for item in schema if item != "null")
        return from_json(branch, value)
    if isinstance(schema, dict):
        kind = schema.get("type")
        if kind == "record":
            if not isinstance(value, dict):
                raise InvalidEvent(f"{schema.get('name')}: expected an object, got {value!r}")
            return {
                field["name"]: from_json(field["type"], value.get(field["name"]))
                for field in schema["fields"]
            }
        if kind == "array":
            return [from_json(schema["items"], item) for item in value or []]
        if schema.get("logicalType") in ("timestamp-millis", "timestamp-micros"):
            if isinstance(value, str):
                return datetime.fromisoformat(value)
            return value
        return from_json(kind, value)
    return value


def check(name: str, record: dict[str, Any]) -> None:
    """Raise `InvalidEvent` unless `record` is a valid value for topic `name`."""
    schema = parsed_schema(name)
    try:
        validate(from_json(schema, to_json(record)), schema, raise_errors=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise InvalidEvent(f"{name}: {exc}") from exc
