"""A Jaeger trace boiled down to what an admin can act on (spec §3)."""

from datetime import UTC, datetime

import pytest

from app.modules.monitoring.traces import Failure, to_failure

pytestmark = pytest.mark.unit

T0 = 1_763_380_800_000_000  # 2025-11-17T12:00:00Z in microseconds


def _span(span_id, op, start, parent=None, tags=(), logs=(), process="p1"):
    return {
        "spanID": span_id, "operationName": op, "startTime": start, "processID": process,
        "references": [{"refType": "CHILD_OF", "spanID": parent}] if parent else [],
        "tags": [{"key": k, "value": v} for k, v in tags], "logs": list(logs),
    }


def _trace(spans, processes=None):
    return {"traceID": "t1", "spans": spans,
            "processes": processes or {"p1": {"serviceName": "part-a-worker"}}}


def test_the_root_and_the_first_failed_step_with_its_exception_message():
    trace = _trace([
        _span("b", "RunActivity:billing.precheck", T0 + 50, parent="a",
              tags=[("error", True)],
              logs=[{"fields": [{"key": "event", "value": "exception"},
                                {"key": "exception.message", "value": "gateway unavailable"}]}]),
        _span("a", "RunWorkflow:BookAppointmentWorkflow", T0),
    ])
    assert to_failure(trace) == Failure(
        trace_id="t1", at=datetime.fromtimestamp(T0 / 1e6, UTC), service="part-a-worker",
        operation="RunWorkflow:BookAppointmentWorkflow",
        failed_step="RunActivity:billing.precheck", message="gateway unavailable",
        trace_path="/jaeger/trace/t1",
    )


def test_without_an_exception_the_status_description_is_the_message():
    trace = _trace([_span("a", "POST /appointments", T0, tags=[
        ("error", True), ("otel.status_description", "slot taken")])])
    failure = to_failure(trace)
    assert failure.failed_step == "POST /appointments" and failure.message == "slot taken"


def test_a_trace_with_no_error_span_is_not_a_failure():
    assert to_failure(_trace([_span("a", "GET /slots", T0)])) is None


def test_the_earliest_error_span_is_the_failed_step():
    trace = _trace([
        _span("c", "late", T0 + 90, parent="a", tags=[("error", True)]),
        _span("b", "early", T0 + 10, parent="a", tags=[("error", True)]),
        _span("a", "root", T0),
    ])
    assert to_failure(trace).failed_step == "early"


def test_no_message_at_all_says_so():
    trace = _trace([_span("a", "root", T0, tags=[("error", True)])])
    assert to_failure(trace).message == "no error message recorded"
