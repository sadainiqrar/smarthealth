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
        (answers, failed), jaeger = await asyncio.gather(self._ask_all(w), self._jaeger_status())
        if self._prometheus is None:
            source = "not_configured"
        else:
            # Unavailable only when nothing answered; a partial failure blanks just its cards.
            source = "unavailable" if failed and not answers else "ok"
        view = _View(answers, failed, blind=source != "ok")
        result = Overview(
            window_hours=hours,
            generated_at=datetime.fromtimestamp(self._clock(), UTC),
            sources=Sources(prometheus=source, jaeger=jaeger),
            components=self._components(view),
            flows=self._flows(view, w),
            backlogs=self._backlogs(view, w),
            ai=self._ai(view, w),
        )
        return self._store(("overview", hours), result)

    async def _jaeger_status(self) -> str:
        if self._jaeger is None:
            return "not_configured"
        try:
            await self._jaeger.ping()
        except SourceUnavailable:
            return "unavailable"
        return "ok"

    async def _ask_all(self, w: str) -> tuple[dict[str, list[Sample]], set[str]]:
        """Every question, once, in parallel. Returns the answers and the set of queries that
        failed, so a card is "unknown" only when one of its own queries did."""
        if self._prometheus is None:
            return {}, set()
        asked = list(dict.fromkeys(q for q in queries.every_query(w) if q))
        prometheus = self._prometheus

        async def ask(promql: str) -> tuple[str, list[Sample] | None]:
            try:
                return promql, await prometheus.query(promql)
            except SourceUnavailable:
                return promql, None

        pairs = await asyncio.gather(*(ask(q) for q in asked))
        answers = {q: samples for q, samples in pairs if samples is not None}
        return answers, {q for q, samples in pairs if samples is None}

    @staticmethod
    def _components(v: _View) -> list[Component]:
        ages = {s.labels.get("job"): s.value for s in v.samples(queries.HEARTBEAT_AGE)}
        out = []
        for job, name in queries.COMPONENTS:
            verdict = v.unknown_if(queries.HEARTBEAT_AGE) or rules.heartbeat(ages.get(job))
            out.append(Component(name=name, job=job, **asdict(verdict)))
        for broker, q in queries.BROKER_UP.items():
            up = v.scalar(q)
            if blind := v.unknown_if(q):
                verdict = blind
            elif up and up > 0:
                verdict = rules.Verdict("ok", "up")
            else:
                verdict = rules.Verdict("bad", "down or not reporting")
            out.append(Component(name="RabbitMQ" if broker == "rabbitmq" else "Kafka",
                                 job=broker, **asdict(verdict)))
        return out

    @staticmethod
    def _flows(v: _View, w: str) -> list[FlowRead]:
        out = []
        for flow in queries.FLOWS:
            q_total, q_failed = queries.flow_total(flow, w), queries.flow_failed(flow, w)
            q_prev, q_p95 = queries.flow_total(flow, w, w), queries.flow_p95_ms(flow, w)
            total, failed = v.scalar(q_total), v.scalar(q_failed)
            p95, prev = v.number(q_p95), v.scalar(q_prev)
            verdict = v.unknown_if(q_total, q_failed) or rules.failure_share(total, failed)
            out.append(FlowRead(
                key=flow.key, label=flow.label,
                total=_whole(total), failed=_whole(failed),
                failure_pct=(round(failed / total * 100, 1) if total and failed is not None
                             else None),
                p95_seconds=None if p95 is None else round(p95 / 1000, 2),
                previous_total=_whole(prev),
                **asdict(verdict),
            ))
        return out

    @staticmethod
    def _backlogs(v: _View, w: str) -> list[Backlog]:
        out = []
        for key, label, rule, q in queries.backlogs(w):
            value = v.scalar(q)
            verdict = v.unknown_if(q) or rules.level(rule, value)
            out.append(Backlog(key=key, label=label, value=value, **asdict(verdict)))
        return out

    @staticmethod
    def _ai(v: _View, w: str) -> AiHealth:
        qs = queries.ai(w)
        a = {k: v.scalar(q) for k, q in qs.items()}
        turns, errors = a["turns"], a["errors"]
        first_word = v.number(qs["first_token_p95_ms"])
        first_word_s = None if first_word is None else round(first_word / 1000, 2)
        no_results = (round(a["no_results"] / a["searches"] * 100, 1)
                      if a["searches"] and a["no_results"] is not None else None)
        if blind := v.unknown_if(*qs.values()):
            verdict = blind
        elif not turns:
            verdict = rules.Verdict("idle", "no assistant turns in this window")
        else:
            parts = [rules.failure_share(turns, errors)]
            if first_word_s is not None:
                parts.append(rules.level("first_token_p95_s", first_word_s))
            if no_results is not None:
                parts.append(rules.level("retrieval_no_results_pct", no_results))
            status = rules.worst(p.status for p in parts)
            verdict = rules.Verdict(status, next(p.reason for p in parts if p.status == status))
        return AiHealth(
            turns=_whole(turns), errors=_whole(errors),
            error_pct=round(errors / turns * 100, 1) if turns and errors is not None else None,
            first_token_p95_seconds=first_word_s, retrieval_no_results_pct=no_results,
            tokens=_whole(a["tokens"]), **asdict(verdict),
        )

    # -- failures ---------------------------------------------------------------------------

    async def failures(self, hours: int, limit: int) -> FailuresRead:
        if (hit := self._cached(("failures", hours, limit))) is not None:
            return hit
        if self._jaeger is None:
            return FailuresRead(window_hours=hours, source="not_configured", failures=[])
        end = datetime.fromtimestamp(self._clock(), UTC)
        start = end - timedelta(hours=hours)
        replies = await asyncio.gather(
            *(self._jaeger.error_traces(s, start, end, limit) for s in TRACED_SERVICES),
            return_exceptions=True)
        for reply in replies:
            if isinstance(reply, BaseException) and not isinstance(reply, SourceUnavailable):
                raise reply
        batches = [r for r in replies if not isinstance(r, BaseException)]
        if not batches:
            return self._store(("failures", hours, limit),
                               FailuresRead(window_hours=hours, source="unavailable", failures=[]))
        seen: dict[str, FailureRead] = {}
        for trace in (t for batch in batches for t in batch):
            try:
                failure = to_failure(trace)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # one malformed trace must not hide the others
            if failure is not None and failure.trace_id not in seen:
                seen[failure.trace_id] = FailureRead(**asdict(failure))
        ordered = sorted(seen.values(), key=lambda f: f.at, reverse=True)[:limit]
        return self._store(("failures", hours, limit),
                           FailuresRead(window_hours=hours, source="ok", failures=ordered))


def _whole(value: float | None) -> int | None:
    return None if value is None else round(value)


class _View:
    """What Prometheus answered, and which questions it could not."""

    def __init__(self, answers: dict[str, list[Sample]], failed: set[str], blind: bool) -> None:
        self._answers = answers
        self._failed = failed
        self._blind = blind  # nothing answered, or no Prometheus: every card is unknown

    def samples(self, promql: str | None) -> list[Sample]:
        return self._answers.get(promql, []) if promql else []

    def scalar(self, promql: str | None) -> float | None:
        samples = self.samples(promql)
        return samples[0].value if samples else None

    def number(self, promql: str | None) -> float | None:
        """Like `scalar`, but NaN (a quantile of nothing) is no figure."""
        value = self.scalar(promql)
        return None if value is None or value != value else value

    def unknown_if(self, *promql: str | None) -> rules.Verdict | None:
        if self._blind or any(q in self._failed for q in promql if q):
            return rules.Verdict("unknown", "metrics unavailable")
        return None
