import json
import logging
from datetime import UTC, datetime

import pytest

from app.core.clock import Clock, FixedClock, get_clock
from app.core.logging import JsonFormatter, configure_logging
from app.core.registry import Registry

pytestmark = pytest.mark.unit


def test_clock_returns_timezone_aware_utc():
    """A naive datetime silently compares wrong against a timestamptz column."""
    moment = Clock().now()
    assert moment.tzinfo is not None
    assert moment.utcoffset().total_seconds() == 0


def test_fixed_clock_is_deterministic():
    instant = datetime(2026, 9, 2, 12, 30, tzinfo=UTC)
    clock = FixedClock(instant)
    assert clock.now() == instant
    assert clock.now() == instant


def test_get_clock_returns_a_usable_default():
    assert isinstance(get_clock().now(), datetime)


def test_registry_registers_and_retrieves():
    registry: Registry[str] = Registry("consumer")
    registry.register("appointments.booked", "handler-a")
    assert registry.get("appointments.booked") == "handler-a"
    assert registry.names() == ["appointments.booked"]
    assert "appointments.booked" in registry
    assert len(registry) == 1


def test_registry_rejects_a_duplicate_name():
    """Two handlers silently claiming one name is how events get processed twice."""
    registry: Registry[str] = Registry("consumer")
    registry.register("a", "first")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", "second")


def test_registry_reports_known_names_when_lookup_fails():
    registry: Registry[str] = Registry("consumer")
    registry.register("known", "x")
    with pytest.raises(KeyError, match="known"):
        registry.get("missing")


def test_registry_names_are_sorted():
    registry: Registry[str] = Registry("task")
    for name in ("c", "a", "b"):
        registry.register(name, name)
    assert registry.names() == ["a", "b", "c"]


def test_json_formatter_emits_parseable_records():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="booking %s confirmed", args=("abc",), exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "booking abc confirmed"
    assert "timestamp" in payload


def test_json_formatter_includes_exception_text():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_installs_the_json_formatter():
    configure_logging(level="WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
