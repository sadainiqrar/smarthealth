"""The health page's figures (system health spec §4): ask every question in parallel,
apply the rules, cache the answer for 15 s. Never raises because a source is down -- the
answer says which source is down instead.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.modules.monitoring import queries, rules
from app.modules.monitoring.client import (
    JaegerClient,
    PrometheusClient,
    Sample,
    SourceUnavailable,
)
from app.modules.monitoring.schemas import (
    AiHealth,
    Backlog,
    Component,
    FailureRead,
    FailuresRead,
    FlowRead,
    Overview,
    Sources,
)
from app.modules.monitoring.traces import to_failure
from app.settings import Settings

CACHE_SECONDS = 15
#: The services whose error traces the page lists (their OTel service.name).
TRACED_SERVICES = ("part-a-api", "part-a-worker", "part-a-consumers", "part-a-celery",
                   "knowledge", "assistant")


class Prometheus(Protocol):
    async def query(self, promql: str) -> list[Sample]: ...


class Jaeger(Protocol):
    async def error_traces(self, service: str, start: datetime, end: datetime,
                           limit: int) -> list[dict]: ...

    async def ping(self) -> None: ...


class MonitoringService:
    def __init__(
        self,
        prometheus: Prometheus | None,
        jaeger: Jaeger | None,
        clock: Callable[[], float] = time.time,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._prometheus = prometheus
        self._jaeger = jaeger
        self._clock = clock
        self._http = http
        self._cache: dict[tuple, tuple[float, object]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> MonitoringService:
        http = httpx.AsyncClient()
        return cls(
            PrometheusClient(settings.prometheus_url, http) if settings.prometheus_url else None,
            JaegerClient(settings.jaeger_query_url, http) if settings.jaeger_query_url else None,
            http=http,
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _cached(self, key: tuple):
        hit = self._cache.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        return None

    def _store(self, key: tuple, value):
        self._cache[key] = (self._clock() + CACHE_SECONDS, value)
        return value

    # -- overview ---------------------------------------------------------------------------

    async def overview(self, hours: int) -> Overview:
        if (hit := self._cached(("overview", hours))) is not None:
            return hit
        w = f"{hours}h"
        down = False

        async def ask(promql: str | None) -> list[Sample] | None:
            nonlocal down
            if promql is None or self._prometheus is None:
                return None
            try:
                return await self._prometheus.query(promql)
            except SourceUnavailable:
                down = True
                return None

        def scalar(samples: list[Sample] | None) -> float | None:
            return samples[0].value if samples else None

        flows = queries.FLOWS
        flow_qs = [(queries.flow_total(f, w), queries.flow_total(f, w, w),
                    queries.flow_failed(f, w), queries.flow_p95_ms(f, w)) for f in flows]
        backlog_qs = queries.backlogs(w)
        ai_qs = queries.ai(w)
        asked = [queries.HEARTBEAT_AGE, *queries.BROKER_UP.values()]
        asked += [q for group in flow_qs for q in group]
        asked += [q for *_, q in backlog_qs]
        asked += list(ai_qs.values())

        async def jaeger_status() -> str:
            if self._jaeger is None:
                return "not_configured"
            try:
                await self._jaeger.ping()
            except SourceUnavailable:
                return "unavailable"
            return "ok"

        *replies, jaeger_source = await asyncio.gather(*(ask(q) for q in asked), jaeger_status())
        answers = dict(zip(range(len(asked)), replies, strict=True))
        answer = {q: answers[i] for i, q in enumerate(asked) if q is not None}

        if self._prometheus is None:
            source = "not_configured"
        else:
            source = "unavailable" if down else "ok"
        blind = source != "ok"

        def unknown() -> rules.Verdict:
            return rules.Verdict("unknown", "metrics unavailable")

        # Components
        ages = {s.labels.get("job"): s.value for s in (answer.get(queries.HEARTBEAT_AGE) or [])}
        components = []
        for job, name in queries.COMPONENTS:
            v = unknown() if blind else rules.heartbeat(ages.get(job))
            components.append(Component(name=name, job=job, **asdict(v)))
        for broker, q in queries.BROKER_UP.items():
            up = scalar(answer.get(q))
            if blind:
                v = unknown()
            elif up and up > 0:
                v = rules.Verdict("ok", "up")
            else:
                v = rules.Verdict("bad", "down or not reporting")
            components.append(Component(name="RabbitMQ" if broker == "rabbitmq" else "Kafka",
                                        job=broker, **asdict(v)))

        # Flows
        flow_reads = []
        for flow, (q_total, q_prev, q_failed, q_p95) in zip(flows, flow_qs, strict=True):
            total = scalar(answer.get(q_total))
            failed = scalar(answer.get(q_failed))
            p95 = scalar(answer.get(q_p95)) if q_p95 else None
            v = unknown() if blind else rules.failure_share(total, failed)
            flow_reads.append(FlowRead(
                key=flow.key, label=flow.label,
                total=None if total is None else round(total),
                failed=None if failed is None else round(failed),
                failure_pct=(round(failed / total * 100, 1) if total and failed is not None
                             else None),
                p95_seconds=None if p95 is None or p95 != p95 else round(p95 / 1000, 2),
                previous_total=(None if (prev := scalar(answer.get(q_prev))) is None
                                else round(prev)),
                **asdict(v),
            ))

        # Backlogs
        backlog_reads = []
        for key, label, rule, q in backlog_qs:
            value = scalar(answer.get(q))
            v = unknown() if blind else rules.level(rule, value)
            backlog_reads.append(Backlog(key=key, label=label, value=value, **asdict(v)))

        # AI
        a = {k: scalar(answer.get(q)) for k, q in ai_qs.items()}
        turns, errors = a["turns"], a["errors"]
        error_pct = round(errors / turns * 100, 1) if turns and errors is not None else None
        ftp = a["first_token_p95_ms"]
        ftp_s = None if ftp is None or ftp != ftp else round(ftp / 1000, 2)
        no_results = (round(a["no_results"] / a["searches"] * 100, 1)
                      if a["searches"] and a["no_results"] is not None else None)
        if blind:
            ai_v = unknown()
        elif not turns:
            ai_v = rules.Verdict("idle", "no assistant turns in this window")
        else:
            parts = [rules.failure_share(turns, errors)]
            if ftp_s is not None:
                parts.append(rules.level("first_token_p95_s", ftp_s))
            if no_results is not None:
                parts.append(rules.level("retrieval_no_results_pct", no_results))
            status = rules.worst(p.status for p in parts)
            reason = next(p.reason for p in parts if p.status == status)
            ai_v = rules.Verdict(status, reason)
        ai = AiHealth(
            turns=None if turns is None else round(turns),
            errors=None if errors is None else round(errors),
            error_pct=error_pct, first_token_p95_seconds=ftp_s,
            retrieval_no_results_pct=no_results,
            tokens=None if a["tokens"] is None else round(a["tokens"]),
            **asdict(ai_v),
        )

        result = Overview(
            window_hours=hours,
            generated_at=datetime.fromtimestamp(self._clock(), UTC),
            sources=Sources(prometheus=source,
                            jaeger=jaeger_source),
            components=components, flows=flow_reads, backlogs=backlog_reads, ai=ai,
        )
        return self._store(("overview", hours), result)

    # -- failures ---------------------------------------------------------------------------

    async def failures(self, hours: int, limit: int) -> FailuresRead:
        if (hit := self._cached(("failures", hours, limit))) is not None:
            return hit
        if self._jaeger is None:
            return FailuresRead(window_hours=hours, source="not_configured", failures=[])
        end = datetime.fromtimestamp(self._clock(), UTC)
        start = end - timedelta(hours=hours)
        try:
            batches = await asyncio.gather(*(
                self._jaeger.error_traces(s, start, end, limit) for s in TRACED_SERVICES))
        except SourceUnavailable:
            return FailuresRead(window_hours=hours, source="unavailable", failures=[])
        seen: dict[str, FailureRead] = {}
        for trace in (t for batch in batches for t in batch):
            try:
                failure = to_failure(trace)
            except (KeyError, TypeError, ValueError):
                continue  # one malformed trace must not hide the others
            if failure is not None and failure.trace_id not in seen:
                seen[failure.trace_id] = FailureRead(**asdict(failure))
        ordered = sorted(seen.values(), key=lambda f: f.at, reverse=True)[:limit]
        return self._store(("failures", hours, limit),
                           FailuresRead(window_hours=hours, source="ok", failures=ordered))
