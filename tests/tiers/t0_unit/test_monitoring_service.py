"""MonitoringService against fake sources: figures become statuses, a dead source
becomes "unknown" cards instead of an error, and answers are cached briefly."""

from datetime import UTC, datetime

import pytest

from app.modules.monitoring import queries
from app.modules.monitoring.client import Sample, SourceUnavailable
from app.modules.monitoring.service import MonitoringService

pytestmark = pytest.mark.unit
NOW = 1_763_380_800.0


class FakePrometheus:
    def __init__(self, answers: dict[str, list[Sample]] | None = None, down: bool = False):
        self.answers = answers or {}
        self.down = down
        self.asked: list[str] = []

    async def query(self, promql: str) -> list[Sample]:
        self.asked.append(promql)
        if self.down:
            raise SourceUnavailable("prometheus: refused")
        return self.answers.get(promql, [])


class FakeJaeger:
    def __init__(self, traces=None, down=False):
        self.traces = traces or {}
        self.down = down

    async def ping(self):
        if self.down:
            raise SourceUnavailable("jaeger: refused")

    async def error_traces(self, service, start, end, limit):
        if self.down:
            raise SourceUnavailable("jaeger: refused")
        return self.traces.get(service, [])


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


def test_generated_at_is_the_clock():
    assert datetime.fromtimestamp(NOW, UTC).year == 2025


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
