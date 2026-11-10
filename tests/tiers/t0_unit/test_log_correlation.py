"""A JSON log line written inside a span names that span, so a log leads to its trace."""

import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.core.logging import JsonFormatter

pytestmark = pytest.mark.unit


def _record() -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)


def test_a_record_inside_a_span_carries_its_trace_and_span_ids():
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("work") as span:
        line = json.loads(JsonFormatter().format(_record()))
        context = span.get_span_context()
    assert line["trace_id"] == format(context.trace_id, "032x")
    assert line["span_id"] == format(context.span_id, "016x")


def test_a_record_outside_any_span_has_no_trace_fields():
    with trace.use_span(trace.INVALID_SPAN):
        line = json.loads(JsonFormatter().format(_record()))
    assert "trace_id" not in line
    assert "span_id" not in line
