"""Fake Prometheus and Jaeger for the monitoring tests (T0 service tests, T1 contract tests)."""

from __future__ import annotations

from app.modules.monitoring.client import Sample, SourceUnavailable


class FakePrometheus:
    """Answers each PromQL string from `answers` (an empty list when absent). `down` refuses
    every query; `fail` refuses just the named ones."""

    def __init__(self, answers: dict[str, list[Sample]] | None = None, down: bool = False,
                 fail: set[str] | None = None):
        self.answers = answers or {}
        self.down = down
        self.fail = fail or set()
        self.asked: list[str] = []

    async def query(self, promql: str) -> list[Sample]:
        self.asked.append(promql)
        if self.down or promql in self.fail:
            raise SourceUnavailable("prometheus: refused")
        return self.answers.get(promql, [])


class FakeJaeger:
    """`traces` maps a service name to its traces. `down` refuses everything; `down_services`
    refuses `error_traces` for the named services only."""

    def __init__(self, traces=None, down=False, down_services: set[str] | None = None):
        self.traces = traces or {}
        self.down = down
        self.down_services = down_services or set()

    async def ping(self):
        if self.down:
            raise SourceUnavailable("jaeger: refused")

    async def error_traces(self, service, start, end, limit):
        if self.down or service in self.down_services:
            raise SourceUnavailable("jaeger: refused")
        return self.traces.get(service, [])
