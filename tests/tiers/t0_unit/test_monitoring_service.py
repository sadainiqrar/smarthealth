"""MonitoringService against fake sources: figures become statuses, a dead source
becomes "unknown" cards instead of an error, and answers are cached briefly."""

from datetime import UTC, datetime

import pytest

from app.modules.monitoring import queries
from app.modules.monitoring.client import Sample
from app.modules.monitoring.service import MonitoringService
from tests.harness.monitoring import FakeJaeger, FakePrometheus

pytestmark = pytest.mark.unit
NOW = 1_763_380_800.0


def _one(value: float) -> list[Sample]:
    return [Sample(labels={}, value=value)]


async def test_a_healthy_system_reads_ok_everywhere_it_has_data():
    w = "24h"
    answers = {
        queries.HEARTBEAT_AGE: [Sample({"job": job}, 4.0) for job, _ in queries.COMPONENTS],
        queries.BROKER_UP["rabbitmq"]: _one(1),
        queries.BROKER_UP["kafka"]: _one(1),
        queries.flow_total(queries.FLOWS[0], w): _one(40),
        queries.flow_failed(queries.FLOWS[0], w): _one(1),
    }
    service = MonitoringService(FakePrometheus(answers), FakeJaeger(), clock=lambda: NOW)
    overview = await service.overview(hours=24)
    assert overview.sources.prometheus == "ok"
    assert {c.status for c in overview.components} == {"ok"}
    booking = next(f for f in overview.flows if f.key == "booking")
    assert (booking.total, booking.failed, booking.status) == (40, 1, "ok")
    assert booking.reason == "1 of 40 failed (2.5%)"


async def test_a_component_without_a_heartbeat_is_down():
    service = MonitoringService(FakePrometheus(), FakeJaeger(), clock=lambda: NOW)
    overview = await service.overview(hours=24)
    api = next(c for c in overview.components if c.job == "smarthealth/part-a-api")
    assert (api.status, api.reason) == ("bad", "not reporting")


async def test_prometheus_down_gives_unknown_cards_not_an_error():
    service = MonitoringService(FakePrometheus(down=True), FakeJaeger(), clock=lambda: NOW)
    overview = await service.overview(hours=24)
    assert overview.sources.prometheus == "unavailable"
    assert {f.status for f in overview.flows} == {"unknown"}
    assert {c.status for c in overview.components} == {"unknown"}
    assert overview.ai.status == "unknown"


async def test_not_configured_sources_say_so():
    service = MonitoringService(None, None, clock=lambda: NOW)
    overview = await service.overview(hours=24)
    failures = await service.failures(hours=24, limit=10)
    assert overview.sources.prometheus == "not_configured"
    assert failures.source == "not_configured" and failures.failures == []


async def test_failures_are_newest_first_across_services():
    def trace(tid, start_us):
        return {"traceID": tid, "processes": {"p": {"serviceName": "x"}}, "spans": [
            {"spanID": "a", "operationName": "op", "startTime": start_us, "processID": "p",
             "references": [], "tags": [{"key": "error", "value": True}], "logs": []}]}

    jaeger = FakeJaeger({"part-a-api": [trace("old", 1_000_000)],
                         "assistant": [trace("new", 9_000_000), trace("old", 1_000_000)]})
    service = MonitoringService(FakePrometheus(), jaeger, clock=lambda: NOW)
    result = await service.failures(hours=24, limit=10)
    assert [f.trace_id for f in result.failures] == ["new", "old"]  # de-duplicated


async def test_jaeger_down_is_reported_not_raised():
    service = MonitoringService(FakePrometheus(), FakeJaeger(down=True), clock=lambda: NOW)
    result = await service.failures(hours=24, limit=10)
    assert result.source == "unavailable" and result.failures == []


async def test_answers_are_cached_for_fifteen_seconds():
    now = [NOW]
    prom = FakePrometheus()
    service = MonitoringService(prom, FakeJaeger(), clock=lambda: now[0])
    await service.overview(hours=24)
    asked = len(prom.asked)
    await service.overview(hours=24)
    assert len(prom.asked) == asked  # served from the cache
    now[0] += 16
    await service.overview(hours=24)
    assert len(prom.asked) == 2 * asked


async def test_generated_at_is_the_clock():
    service = MonitoringService(FakePrometheus(), FakeJaeger(), clock=lambda: NOW)
    overview = await service.overview(24)
    assert overview.generated_at == datetime.fromtimestamp(NOW, UTC)


W = "24h"
BOOKING = queries.FLOWS[0]
ASSISTANT = next(f for f in queries.FLOWS if f.key == "assistant")
AI = queries.ai(W)


def _quiet() -> dict[str, list[Sample]]:
    """Every flow answered with zero runs, as the real `or vector(0)` queries do."""
    out = {}
    for flow in queries.FLOWS:
        out[queries.flow_total(flow, W)] = _one(0)
        out[queries.flow_failed(flow, W)] = _one(0)
    return out


def _service(answers=None, **kw):
    return MonitoringService(FakePrometheus(answers, **kw), FakeJaeger(), clock=lambda: NOW)


async def test_one_failed_query_blanks_only_its_own_card():
    answers = _quiet() | {queries.flow_total(BOOKING, W): _one(40)}
    reschedule = queries.FLOWS[1]
    answers.pop(queries.flow_total(reschedule, W))
    overview = await _service(
        answers, fail={queries.flow_total(reschedule, W)}).overview(24)
    by_key = {f.key: f for f in overview.flows}
    assert by_key["booking"].status == "ok" and by_key["booking"].total == 40
    assert by_key["reschedule"].status == "unknown"
    assert by_key["cancellation"].status == "idle"  # answered: nothing ran
    assert overview.sources.prometheus == "ok"
    assert {c.status for c in overview.components} == {"bad"}  # no heartbeat answered, not unknown


async def test_a_failed_heartbeat_query_makes_every_process_unknown_but_not_the_flows():
    overview = await _service(_quiet(), fail={queries.HEARTBEAT_AGE}).overview(24)
    processes = [c for c in overview.components if c.job.startswith("smarthealth/")]
    assert {c.status for c in processes} == {"unknown"}
    assert {f.status for f in overview.flows} == {"idle"}
    assert overview.sources.prometheus == "ok"


async def test_every_query_failing_is_unavailable():
    overview = await _service(down=True).overview(24)
    assert overview.sources.prometheus == "unavailable"


async def test_the_previous_window_has_no_fallback_so_unanswered_means_no_comparison():
    answers = {queries.flow_total(BOOKING, W): _one(40), queries.flow_failed(BOOKING, W): _one(0)}
    overview = await _service(answers).overview(24)
    booking = next(f for f in overview.flows if f.key == "booking")
    assert booking.total == 40 and booking.previous_total is None
    # Inside, a born-in-window term may fall back to zero; the figure as a whole may not.
    assert not queries.flow_total(BOOKING, W, W).endswith("or vector(0)")
    assert queries.flow_total(BOOKING, W).endswith("or vector(0)")


RESCHEDULE = next(f for f in queries.FLOWS if f.key == "reschedule")


def test_the_previous_workflow_window_survives_a_missing_failed_series():
    """Temporal creates `temporal_workflow_failed_total{workflow_type=...}` only at the
    first failure, and `completed + <nothing>` is nothing. So the previous window is
    completed runs + (failed runs or zero): a never-failed workflow still compares, and a
    missing `completed` still means no comparison. Two metrics added, never one
    `__name__=~` selector: min_over_time drops the name and the two series would collide."""
    sel = f'{{workflow_type="{RESCHEDULE.name}"}}'
    completed = queries.runs(f"temporal_workflow_completed_total{sel}", W, W)
    failed = queries.runs(f"temporal_workflow_failed_total{sel}", W, W)
    assert queries.flow_total(RESCHEDULE, W, W) == f"{completed} + ({failed} or vector(0))"
    assert "__name__" not in queries.flow_total(RESCHEDULE, W, W)
    completed_now = queries.runs(f"temporal_workflow_completed_total{sel}", W)
    failed_now = queries.runs(f"temporal_workflow_failed_total{sel}", W)
    assert queries.flow_total(RESCHEDULE, W) == (
        f"({completed_now} or vector(0)) + ({failed_now} or vector(0))")


def test_a_series_born_inside_the_window_counts_from_zero():
    """`increase` drops a new series' first sample (the first export of a process, or a
    restart). Each figure adds the first value of every series absent at the window's
    start; the start is the window plus its offset."""
    sel = 'smarthealth_bookings_total{outcome="failed"}'
    now = queries.runs(sel, "6h")
    assert f"increase({sel}[6h])" in now
    assert f"min_over_time({sel}[6h]) unless {sel} offset 6h" in now
    before = queries.runs(sel, "6h", "6h")
    assert f"increase({sel}[6h] offset 6h)" in before
    assert f"min_over_time({sel}[6h] offset 6h) unless {sel} offset 12h" in before
    with pytest.raises(ValueError):
        queries.runs(sel, "90m")


def test_every_counter_figure_counts_born_in_window_series():
    """No counter figure is a bare `increase`: flows, the dead-letter backlog, the AI card."""
    counters = [q for q in queries.every_query(W)
                if "increase(" in q and "histogram_quantile" not in q]
    assert counters and all("min_over_time(" in q for q in counters)


def test_booking_is_counted_by_outcome_not_by_its_workflow():
    """A compensated booking ends `failed` while BookAppointmentWorkflow completes
    normally; the workflow's own counters would say "none failed"."""
    assert BOOKING.kind == "booking"
    assert 'smarthealth_bookings_total{outcome=~"confirmed|failed"}' in queries.flow_total(
        BOOKING, W)
    assert 'smarthealth_bookings_total{outcome="failed"}' in queries.flow_failed(BOOKING, W)
    assert "temporal_workflow" not in queries.flow_total(BOOKING, W, W)
    assert 'workflow_type="BookAppointment"' in queries.flow_p95_ms(BOOKING, W)


async def test_a_nan_p95_is_no_figure():
    answers = {queries.flow_p95_ms(BOOKING, W): _one(float("nan"))}
    overview = await _service(answers).overview(24)
    assert next(f for f in overview.flows if f.key == "booking").p95_seconds is None


async def test_a_missing_outbox_answer_is_unknown_not_ok():
    overview = await _service().overview(24)
    outbox = next(b for b in overview.backlogs if b.key == "outbox")
    assert (outbox.status, outbox.value) == ("unknown", None)


async def test_a_slow_first_word_makes_a_healthy_assistant_warn_with_the_latency_reason():
    answers = {AI["turns"]: _one(100), AI["errors"]: _one(1),
               AI["first_token_p95_ms"]: _one(7000)}
    ai = (await _service(answers).overview(24)).ai
    assert ai.status == "warn"
    assert ai.reason == "7 s: falling behind"
    assert ai.first_token_p95_seconds == 7.0 and ai.error_pct == 1.0


async def test_an_assistant_with_no_turns_is_idle():
    ai = (await _service().overview(24)).ai
    assert (ai.status, ai.reason) == ("idle", "no assistant turns in this window")


async def test_one_traced_service_failing_does_not_hide_the_others():
    def trace(tid):
        return {"traceID": tid, "processes": {"p": {"serviceName": "x"}}, "spans": [
            {"spanID": "a", "operationName": "op", "startTime": 9_000_000, "processID": "p",
             "references": [], "tags": [{"key": "error", "value": True}], "logs": []}]}

    jaeger = FakeJaeger({"assistant": [trace("kept")]}, down_services={"part-a-api"})
    result = await MonitoringService(FakePrometheus(), jaeger, clock=lambda: NOW).failures(24, 10)
    assert result.source == "ok" and [f.trace_id for f in result.failures] == ["kept"]


async def test_every_traced_service_failing_is_unavailable_and_that_is_cached_too():
    class Counting(FakeJaeger):
        calls = 0

        async def error_traces(self, *a):
            Counting.calls += 1
            return await super().error_traces(*a)

    service = MonitoringService(FakePrometheus(), Counting(down=True), clock=lambda: NOW)
    first = await service.failures(24, 10)
    calls = Counting.calls
    assert first.source == "unavailable"
    await service.failures(24, 10)
    assert Counting.calls == calls


async def test_a_trace_missing_a_field_the_reducer_reads_is_skipped():
    broken = {"traceID": "bad", "spans": None}
    service = MonitoringService(FakePrometheus(), FakeJaeger({"part-a-api": [broken, "junk"]}),
                                clock=lambda: NOW)
    result = await service.failures(24, 10)
    assert result.source == "ok" and result.failures == []


async def test_the_overview_reports_jaeger_as_it_is():
    up = await MonitoringService(FakePrometheus(), FakeJaeger(), clock=lambda: NOW).overview(24)
    down = await MonitoringService(FakePrometheus(), FakeJaeger(down=True),
                                   clock=lambda: NOW).overview(24)
    assert up.sources.jaeger == "ok"
    assert down.sources.jaeger == "unavailable"
    assert down.sources.prometheus == "ok"  # one dead source does not blind the other


async def test_a_malformed_trace_is_skipped_not_raised():
    good = {"traceID": "good", "processes": {"p": {"serviceName": "x"}}, "spans": [
        {"spanID": "a", "operationName": "op", "startTime": 5_000_000, "processID": "p",
         "references": [], "tags": [{"key": "error", "value": True}], "logs": []}]}
    broken = {"traceID": "bad", "spans": [{"tags": [{"key": "error", "value": True}]}]}
    service = MonitoringService(FakePrometheus(), FakeJaeger({"part-a-api": [broken, good]}),
                                clock=lambda: NOW)
    result = await service.failures(hours=24, limit=10)
    assert result.source == "ok" and [f.trace_id for f in result.failures] == ["good"]
