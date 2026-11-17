# System Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the admin one in-app *System health* page (status of every process, the critical
flows, recent failures with their failed step, backlogs, AI health) and a *System health
snapshot* assistant report that explains the same data in plain English.

**Architecture:** A new Part A module, `app/modules/monitoring/`, reads Prometheus (instant
PromQL queries) and Jaeger (error traces) server-side, turns numbers into plain statuses with
pure rules, and serves two admin-only endpoints. Three small instrumentation additions fill the
gaps:
- a heartbeat gauge in every process;
- Celery task outcomes;
- knowledge search outcomes.

The frontend page and the assistant's new report kind both read the two endpoints, so they
cannot disagree.

**Tech Stack:** FastAPI, httpx, OpenTelemetry metrics SDK, Prometheus HTTP API, Jaeger query
API, Celery signals, gRPC (Part B), React + TanStack Query + shadcn, Vitest, pytest tiers.

**Spec:** `docs/superpowers/specs/2026-11-17-system-health-design.md` (D-55).

**Conventions every task follows:**
- **Branch:** `feat/system-health`, which already exists and holds the spec commit.
- **Commits:** end every commit message with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_014wvVzRD9djETVx2nsVBADr
  ```
- **Fast suite:** `python -m pytest -m "not docker" -q`. Lint: `python -m ruff check app tests services`.
- **Stop hooks:**
  - The E2E gate blocks when `app/modules/**` or `app/core/**` changes without a validated
    case. Task 9 adds the case.
  - The docs gate blocks when app code changes with no doc change. Task 15 covers it.
  - Until those tasks land, commit freely. The hooks only fire at session stop.
- **Metric naming:** the OTel collector's Prometheus exporter turns `a.b` with unit `ms` into
  `a_b_milliseconds`, unit `s` into `a_b_seconds`, and adds `_total` to counters. Every series
  carries `job="smarthealth/<service.name>"`.

---

## File map

| File | Create/Modify | Responsibility |
| --- | --- | --- |
| `app/core/telemetry.py` | Modify | `register_heartbeat(meter_provider)`; called by `setup_telemetry` |
| `app/celery_app.py` | Modify | beat gets telemetry; task-outcome signal handlers |
| `app/core/task_metrics.py` | Create | the Celery outcome counter and the three signal handlers (framework-free) |
| `services/knowledge/knowledge_service/telemetry.py` | Modify | meter provider + heartbeat |
| `services/knowledge/knowledge_service/metrics.py` | Create | `searches` counter, `search_duration` histogram, `record_search` |
| `services/knowledge/knowledge_service/server.py` | Modify | record each Search |
| `services/assistant/assistant_service/telemetry.py` | Modify | heartbeat |
| `app/settings.py` | Modify | `prometheus_url`, `jaeger_query_url` |
| `app/modules/monitoring/__init__.py` | Create | empty |
| `app/modules/monitoring/client.py` | Create | `PrometheusClient`, `JaegerClient`, `Sample`, `SourceUnavailable` |
| `app/modules/monitoring/rules.py` | Create | `Verdict`, thresholds, pure status rules |
| `app/modules/monitoring/traces.py` | Create | Jaeger trace JSON → `Failure` |
| `app/modules/monitoring/queries.py` | Create | every PromQL string; components and flows tables |
| `app/modules/monitoring/schemas.py` | Create | Pydantic response models |
| `app/modules/monitoring/service.py` | Create | `MonitoringService` (parallel queries, rules, 15 s cache) |
| `app/modules/monitoring/router.py` | Create | `GET /monitoring/overview`, `GET /monitoring/failures` |
| `app/api/router.py`, `app/main.py` | Modify | mount the router; build/close the service in the lifespan |
| `docker-compose.infra.yml`, `deploy/helm/charts/core/values.yaml` | Modify | the two URLs |
| `services/assistant/assistant_service/parta.py` | Modify | `monitoring_overview`, `monitoring_failures` |
| `services/assistant/assistant_service/reports.py` | Modify | the `system_health` kind |
| `tests/harness/fake_part_a.py` | Modify | the two monitoring endpoints |
| `frontend/src/health.ts` (+ `health.test.ts`) | Create | types and pure display helpers |
| `frontend/src/pages/Health.tsx` | Create | the page |
| `frontend/src/main.tsx`, `frontend/src/pages/Reports.tsx` | Modify | nav and route; the report button |
| `ops/grafana/dashboards/*.json` | Modify | panels for the new metrics |
| `tests/tiers/t0_unit/test_monitoring_rules.py`, `test_monitoring_traces.py`, `test_heartbeat.py`, `test_task_metrics.py` | Create | unit tests |
| `tests/tiers/t1_contract/test_monitoring_contract.py` | Create | the endpoints with fake sources |
| `tests/tiers/t3_integration/test_monitoring_sources.py` | Create | real Prometheus/Jaeger in the test stack |
| `tests/cases/pa-monitoring-001-*.yaml` | Create | validated case for the core-path change |
| `scripts/k3d_smoke.py`, `scripts/aws_check.sh` | Modify | overview check |
| docs (PRD, ARCHITECTURE, README, site, CLAUDE.md, spec) | Modify | D-55, module, endpoints |

---

### Task 0: Align the spec with three build decisions

These come from reading the code while planning:
- **Cache:** the 15 s cache is in-process, not Redis. The contract tier runs without Redis, and a
  per-replica cache is enough for one admin page.
- **Reminders:** reminders are sent by the same `notifications.send` Celery task, so
  "notifications and reminders" is one flow. A per-kind label would need the task to know the
  kind before it runs.
- **Thresholds and the docker test:**
  - Thresholds are "at or above" (`>=`).
  - The AI card reports the first-word p95 (not p50), so it matches its threshold.
  - The LLM provider is judged by the assistant's error share; there's no separate component
    tile.
  - The T3 test proves the PromQL and the Jaeger API shape against the real test stack. The
    tests' own spans never reach Jaeger, because the suite installs in-memory telemetry first.

**Files:** Modify: `docs/superpowers/specs/2026-11-17-system-health-design.md`

- [ ] **Step 1: Edit the spec**

In §4 change the `service.py` row's "15 s Redis cache" to "15 s in-process cache (per replica;
the contract tier has no Redis)".

In §4.1 replace the flows list's `reminders` item and the `notifications` item with:

```
notifications and reminders (Celery `notifications.send`; a reminder is a notification the
visit timer queued, so the two share one task and one flow)
```

In §4.1 change `"first_token_p50_seconds"` to `"first_token_p95_seconds"`. In the Components
sentence, delete "; LLM provider (from LLM call errors)". Add after it: "The LLM provider is
judged in the AI card, by the assistant's error share."

In §4.3 add under the table: "Every threshold is *at or above*: a value equal to `warn` is
`warn`."

In §8 replace the T3 row with:

```
| T3 docker | Against the test stack's real Prometheus and Jaeger: every PromQL query in `queries.py` is accepted (no `SourceUnavailable`); an error span exported over OTLP under a unique service name comes back from `JaegerClient.error_traces` and reduces to a `Failure` with its message |
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-11-17-system-health-design.md
git commit -m "docs(spec): system health -- in-process cache, one notifications flow, T3 scope"
```

---

### Task 1: Heartbeat in every Part A process

**Files:**
- Modify: `app/core/telemetry.py`
- Modify: `app/celery_app.py` (beat gets telemetry)
- Test: `tests/tiers/t0_unit/test_heartbeat.py`

- [ ] **Step 1: Write the failing test**

```python
"""Every process says it is alive, every export interval, whether or not it is busy
(system health spec §3): an idle process otherwise exports nothing and looks dead."""

import time

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.core.telemetry import build_telemetry, register_heartbeat

pytestmark = pytest.mark.unit


def _points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    return [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def test_the_heartbeat_reports_the_current_unix_time():
    reader = InMemoryMetricReader()
    telemetry = build_telemetry("part-a-test", "test", metric_readers=[reader])
    register_heartbeat(telemetry.meter_provider)
    before = time.time()
    points = _points(reader, "smarthealth.heartbeat")
    assert len(points) == 1
    assert before - 1 <= points[0].value <= time.time() + 1


def test_registering_twice_on_one_provider_keeps_one_series():
    reader = InMemoryMetricReader()
    telemetry = build_telemetry("part-a-test", "test", metric_readers=[reader])
    register_heartbeat(telemetry.meter_provider)
    register_heartbeat(telemetry.meter_provider)
    assert len(_points(reader, "smarthealth.heartbeat")) == 1
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_heartbeat.py -q`
Expected: FAIL, `ImportError: cannot import name 'register_heartbeat'`.

- [ ] **Step 3: Implement**

In `app/core/telemetry.py` add `import time` and
`from opentelemetry.metrics import CallbackOptions, Observation` to the imports, then, after
`install`:

```python
#: Providers that already carry a heartbeat; a second registration would double the series.
_heartbeats: set[int] = set()


def register_heartbeat(meter_provider: MeterProvider) -> None:
    """Export `smarthealth.heartbeat` (the process's current Unix time) on every metric
    export (system health spec §3). Prometheus sees `smarthealth_heartbeat_seconds`; a
    process is alive while `time() - smarthealth_heartbeat_seconds` stays under a minute.
    The value is a timestamp, not 1, because the collector re-serves its last value for
    minutes after a process dies -- a frozen timestamp ages, a constant 1 would not."""
    if id(meter_provider) in _heartbeats:
        return
    _heartbeats.add(id(meter_provider))

    def beat(_options: CallbackOptions):
        yield Observation(time.time())

    meter_provider.get_meter("smarthealth.heartbeat").create_observable_gauge(
        "smarthealth.heartbeat", callbacks=[beat], unit="s",
        description="Unix time at the process's last metric export: its liveness",
    )
```

In `setup_telemetry`, directly after `telemetry = install(...)` (before the Celery
instrumentor), add:

```python
    register_heartbeat(telemetry.meter_provider)
```

In `app/celery_app.py`, below the `_telemetry` handler, add the beat process (beat runs no
worker process, so `worker_process_init` never fires there):

```python
@signals.beat_init.connect
def _beat_telemetry(**_kwargs) -> None:
    from app.core.telemetry import setup_telemetry

    setup_telemetry(task_settings(), "part-a-celery-beat")
```

Check compose: `celery-beat` must carry `SMARTHEALTH_OTEL_ENDPOINT_HTTP`. Run
`grep -n "celery-beat" -A 25 docker-compose.infra.yml | grep OTEL`. If that prints nothing, add
`SMARTHEALTH_OTEL_ENDPOINT_HTTP: http://otel-collector:4318` to the service's `environment`.
In the core chart, `deploy/helm/charts/core/values.yaml` line 12 already sets it for every
core workload. Confirm with `grep -n "OTEL" deploy/helm/charts/core/values.yaml`.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/tiers/t0_unit/test_heartbeat.py tests/tiers/t0_unit/test_telemetry.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/core/telemetry.py app/celery_app.py docker-compose.infra.yml tests/tiers/t0_unit/test_heartbeat.py
git commit -m "feat(telemetry): a heartbeat gauge in every Part A process, beat included"
```

---

### Task 2: Celery task outcomes

**Files:**
- Create: `app/core/task_metrics.py`
- Modify: `app/celery_app.py`
- Test: `tests/tiers/t0_unit/test_task_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
"""Celery task outcomes become a metric, so notifications, reminders and document
extraction are visible on the health page (spec §3)."""

import pytest

from app.core import task_metrics

pytestmark = pytest.mark.unit


class _Task:
    name = "notifications.send"


def _count(metric_reader, task: str, outcome: str) -> float:
    data = metric_reader.get_metrics_data()
    total = 0.0
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != "smarthealth.celery.tasks":
                    continue
                for point in metric.data.data_points:
                    if point.attributes == {"task": task, "outcome": outcome}:
                        total += point.value
    return total


@pytest.mark.parametrize(
    ("handler", "outcome"),
    [
        (task_metrics.on_success, "success"),
        (task_metrics.on_retry, "retry"),
        (task_metrics.on_failure, "failure"),
    ],
)
def test_each_signal_counts_its_outcome_by_task_name(metric_reader, handler, outcome):
    before = _count(metric_reader, "notifications.send", outcome)
    if handler is task_metrics.on_retry:
        handler(sender=None, request=type("R", (), {"task": "notifications.send"})())
    else:
        handler(sender=_Task())
    assert _count(metric_reader, "notifications.send", outcome) == before + 1


def test_an_unknown_sender_is_counted_as_unknown(metric_reader):
    before = _count(metric_reader, "unknown", "failure")
    task_metrics.on_failure(sender=None)
    assert _count(metric_reader, "unknown", "failure") == before + 1
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_task_metrics.py -q`
Expected: FAIL, `ImportError: cannot import name 'task_metrics'`.

- [ ] **Step 3: Implement `app/core/task_metrics.py`**

```python
"""Celery task outcomes as a metric (system health spec §3).

Framework-free on purpose (the handlers take Celery's signal keyword arguments, not Celery
types), so `app/core` stays importable without a broker. Connected in `app/celery_app.py`.
Prometheus sees `smarthealth_celery_tasks_total{task, outcome}`; `task` is the registered
task name, a small fixed set.
"""

from __future__ import annotations

from opentelemetry import metrics

_tasks = metrics.get_meter("smarthealth.celery").create_counter(
    "smarthealth.celery.tasks", unit="1",
    description="Celery task runs by task name and outcome (success, retry, failure)",
)


def _name(sender) -> str:
    return getattr(sender, "name", None) or "unknown"


def on_success(sender=None, **_kwargs) -> None:
    _tasks.add(1, {"task": _name(sender), "outcome": "success"})


def on_retry(sender=None, request=None, **_kwargs) -> None:
    # task_retry's sender is the *request*'s task in some Celery versions; the request
    # always carries the name.
    name = getattr(request, "task", None) or _name(sender)
    _tasks.add(1, {"task": name, "outcome": "retry"})


def on_failure(sender=None, **_kwargs) -> None:
    _tasks.add(1, {"task": _name(sender), "outcome": "failure"})
```

In `app/celery_app.py`, after the `_beat_telemetry` handler, add:

```python
from app.core import task_metrics  # noqa: E402 - after the app, beside its other signals

signals.task_success.connect(task_metrics.on_success, weak=False)
signals.task_retry.connect(task_metrics.on_retry, weak=False)
signals.task_failure.connect(task_metrics.on_failure, weak=False)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/tiers/t0_unit/test_task_metrics.py tests/meta/test_celery_tasks.py tests/meta/test_import_boundaries.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/core/task_metrics.py app/celery_app.py tests/tiers/t0_unit/test_task_metrics.py
git commit -m "feat(celery): count task outcomes by task for the health page"
```

---

### Task 3: Knowledge search metrics and heartbeat

**Files:**
- Create: `services/knowledge/knowledge_service/metrics.py`
- Modify: `services/knowledge/knowledge_service/telemetry.py`, `services/knowledge/knowledge_service/server.py`
- Test: `tests/tiers/t1_contract/test_partb_services.py` (add one test)

- [ ] **Step 1: Write the failing test** (append to `tests/tiers/t1_contract/test_partb_services.py`)

```python
async def test_every_search_is_counted_by_outcome(knowledge, metric_reader):
    """System health spec §3: retrieval had traces but no metrics."""
    directory, stub, _ = knowledge

    def outcomes() -> dict[str, float]:
        seen: dict[str, float] = {}
        for rm in metric_reader.get_metrics_data().resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "knowledge.searches":
                        for p in metric.data.data_points:
                            key = p.attributes["outcome"]
                            seen[key] = seen.get(key, 0) + p.value
        return seen

    before = outcomes()
    await stub.Search(knowledge_pb2.SearchRequest(query="nobody-by-this-name"),
                      metadata=_metadata())
    after = outcomes()
    assert after.get("no_results", 0) == before.get("no_results", 0) + 1
```

Before writing it, read the file's existing search test to find the helper that builds the
`authorization` metadata, and use it in place of `_metadata()`:
`grep -n "metadata=" tests/tiers/t1_contract/test_partb_services.py | head -3`.

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t1_contract/test_partb_services.py -k counted -q`
Expected: FAIL, the assertion (no `knowledge.searches` metric).

- [ ] **Step 3: Implement**

`services/knowledge/knowledge_service/metrics.py`:

```python
"""The knowledge service's metrics (system health spec §3).

From the global meter: no-ops until `telemetry.setup` installs a provider, so tests and an
un-exported process pay nothing. Prometheus sees `knowledge_searches_total{outcome}` and
`knowledge_search_duration_milliseconds`.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("knowledge")

searches = _meter.create_counter(
    "knowledge.searches", unit="1",
    description="Searches by outcome: hit, no_results, error",
)
search_duration = _meter.create_histogram(
    "knowledge.search.duration", unit="ms", description="One Search call, start to reply",
)


def record_search(outcome: str, elapsed_ms: float) -> None:
    searches.add(1, {"outcome": outcome})
    search_duration.record(elapsed_ms, {"outcome": outcome})
```

In `server.py` add `import time` and `from knowledge_service import metrics`. Rename the body
of `Search` after the auth and empty-query checks into a helper, and time it:

```python
    async def Search(self, request, context):  # noqa: N802 - gRPC method name
        metadata = dict(context.invocation_metadata())
        try:
            caller = self._verifier.verify(metadata.get("authorization"))
        except Unauthenticated as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        if not request.query.strip():
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is empty")
        started = time.perf_counter()
        try:
            reply = await self._search(request, caller)
        except Exception:
            metrics.record_search("error", (time.perf_counter() - started) * 1000)
            raise
        metrics.record_search("hit" if reply.hits else "no_results",
                              (time.perf_counter() - started) * 1000)
        return reply

    async def _search(self, request, caller) -> knowledge_pb2.SearchReply:
        limit = min(request.limit or DEFAULT_LIMIT, MAX_LIMIT)
        # (the existing index branch and directory branch, unchanged, each `return`ing
        #  its SearchReply)
```

Move the existing lines from `limit = ...` through the final `return` into `_search`
unchanged.

In `telemetry.py`, give knowledge a meter provider and a heartbeat (copy of Part A's, per the
service-independence rule). Add imports:

```python
import time

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
```

and in `setup`, after `trace.set_tracer_provider(provider)`:

```python
    # Metrics (metrics.py) and the heartbeat, every 15 s (system health spec §3).
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"), export_interval_millis=15_000)
    meter_provider = MeterProvider(resource=provider.resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    def beat(_options: CallbackOptions):
        yield Observation(time.time())

    meter_provider.get_meter("knowledge.heartbeat").create_observable_gauge(
        "smarthealth.heartbeat", callbacks=[beat], unit="s",
        description="Unix time at the process's last metric export: its liveness")
```

Check that `services/knowledge/requirements.txt` already lists
`opentelemetry-exporter-otlp-proto-http` (it does), so no new dependency is needed.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/tiers/t1_contract/test_partb_services.py tests/tiers/t1_contract/test_knowledge_retrieval.py tests/meta/test_service_independence.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/knowledge tests/tiers/t1_contract/test_partb_services.py
git commit -m "feat(knowledge): search outcome and duration metrics, and a heartbeat"
```

---

### Task 4: Assistant heartbeat

**Files:** Modify: `services/assistant/assistant_service/telemetry.py`

- [ ] **Step 1: Implement** (no new test: the assistant's `setup` only runs with an endpoint,
  and the T3 and smoke checks in Tasks 13–14 prove the heartbeat arrives)

Add the imports `import time` and `from opentelemetry.metrics import CallbackOptions, Observation`.
Replace the two lines that build and set the meter provider with:

```python
    meter_provider = MeterProvider(resource=provider.resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    def beat(_options: CallbackOptions):
        yield Observation(time.time())

    # The heartbeat (system health spec §3): alive while this keeps moving.
    meter_provider.get_meter("assistant.heartbeat").create_observable_gauge(
        "smarthealth.heartbeat", callbacks=[beat], unit="s",
        description="Unix time at the process's last metric export: its liveness")
```

- [ ] **Step 2: Run the Part B tests**

Run: `python -m pytest tests/tiers/t1_contract/test_partb_services.py tests/meta/test_service_independence.py -q`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add services/assistant/assistant_service/telemetry.py
git commit -m "feat(assistant): heartbeat gauge"
```

---

### Task 5: Settings and the source clients

**Files:**
- Modify: `app/settings.py`
- Create: `app/modules/monitoring/__init__.py` (empty), `app/modules/monitoring/client.py`
- Test: `tests/tiers/t0_unit/test_monitoring_client.py`

- [ ] **Step 1: Write the failing test**

```python
"""The Prometheus and Jaeger clients: what they ask, what they return, and that every
failure becomes one exception the service can turn into "unavailable"."""

from datetime import UTC, datetime

import httpx
import pytest

from app.modules.monitoring.client import (
    JaegerClient,
    PrometheusClient,
    Sample,
    SourceUnavailable,
)

pytestmark = pytest.mark.unit


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_an_instant_query_returns_labelled_samples():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query"
        assert request.url.params["query"] == "up"
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": {"job": "rabbitmq"}, "value": [1.0, "1"]}]}})

    async with _client(handler) as http:
        samples = await PrometheusClient("http://prom:9090", http).query("up")
    assert samples == [Sample(labels={"job": "rabbitmq"}, value=1.0)]


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"status": "error", "error": "parse error"}),
    httpx.Response(200, json={"status": "error", "error": "bad"}),
])
async def test_a_refused_query_is_unavailable(response):
    async with _client(lambda _r: response) as http:
        with pytest.raises(SourceUnavailable):
            await PrometheusClient("http://prom:9090", http).query("up(")


async def test_a_transport_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as http:
        with pytest.raises(SourceUnavailable):
            await PrometheusClient("http://prom:9090", http).query("up")


async def test_jaeger_is_asked_for_error_traces_of_one_service_in_microseconds():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        assert request.url.path == "/jaeger/api/traces"
        return httpx.Response(200, json={"data": [{"traceID": "abc", "spans": [], "processes": {}}]})

    start = datetime(2026, 11, 17, 10, tzinfo=UTC)
    end = datetime(2026, 11, 17, 11, tzinfo=UTC)
    async with _client(handler) as http:
        traces = await JaegerClient("http://jaeger:16686/jaeger", http).error_traces(
            "part-a-api", start, end, 20)
    assert traces == [{"traceID": "abc", "spans": [], "processes": {}}]
    assert seen["service"] == "part-a-api" and seen["tags"] == '{"error":"true"}'
    assert seen["start"] == str(int(start.timestamp() * 1_000_000))
    assert seen["limit"] == "20"


async def test_an_unknown_jaeger_service_is_no_traces_not_an_error():
    # Jaeger answers 200 with errors=[...] and data=null for a service it has never seen.
    response = httpx.Response(200, json={"data": None, "errors": [{"code": 404}]})
    async with _client(lambda _r: response) as http:
        traces = await JaegerClient("http://jaeger:16686", http).error_traces(
            "knowledge", datetime.now(UTC), datetime.now(UTC), 5)
    assert traces == []
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_client.py -q`
Expected: FAIL, `ModuleNotFoundError: app.modules.monitoring`.

- [ ] **Step 3: Implement**

In `app/settings.py`, beside `otel_endpoint_grpc`, add:

```python
    #: Where the health page reads metrics and traces (system health spec §3). Empty means
    #: "not configured", and the page says so instead of failing.
    prometheus_url: str = ""  # e.g. http://prometheus:9090
    jaeger_query_url: str = ""  # e.g. http://jaeger:16686 (in k8s: .../jaeger, its base path)
```

`app/modules/monitoring/__init__.py`: empty file.

`app/modules/monitoring/client.py`:

```python
"""Thin clients for the two monitoring sources (system health spec §3).

Both raise `SourceUnavailable` for anything other than a usable answer, so the service has
one thing to catch and the page one thing to say: "metrics unavailable".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import httpx

TIMEOUT_SECONDS = 3.0


class SourceUnavailable(Exception):
    """The source did not answer, or refused the question."""


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


class PrometheusClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def query(self, promql: str) -> list[Sample]:
        """An instant query: one value per series, now."""
        try:
            response = await self._http.get(
                f"{self._base}/api/v1/query", params={"query": promql}, timeout=TIMEOUT_SECONDS)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"prometheus: {exc}") from exc
        if response.status_code != 200 or body.get("status") != "success":
            raise SourceUnavailable(f"prometheus: {body.get('error', response.status_code)}")
        return [
            Sample(labels=dict(r["metric"]), value=float(r["value"][1]))
            for r in body["data"]["result"]
        ]


class JaegerClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def error_traces(
        self, service: str, start: datetime, end: datetime, limit: int
    ) -> list[dict]:
        """Traces of `service` holding a span tagged error=true, newest first as Jaeger
        returns them. This is the API Jaeger's own UI uses (spec §10: pinned to 1.62)."""
        params = {
            "service": service,
            "tags": json.dumps({"error": "true"}, separators=(",", ":")),
            "start": str(int(start.timestamp() * 1_000_000)),
            "end": str(int(end.timestamp() * 1_000_000)),
            "limit": str(limit),
        }
        try:
            response = await self._http.get(
                f"{self._base}/api/traces", params=params, timeout=TIMEOUT_SECONDS)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"jaeger: {exc}") from exc
        if response.status_code != 200:
            raise SourceUnavailable(f"jaeger: HTTP {response.status_code}")
        return body.get("data") or []
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_client.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/settings.py app/modules/monitoring tests/tiers/t0_unit/test_monitoring_client.py
git commit -m "feat(monitoring): Prometheus and Jaeger clients"
```

---

### Task 6: Status rules

**Files:**
- Create: `app/modules/monitoring/rules.py`
- Test: `tests/tiers/t0_unit/test_monitoring_rules.py`

- [ ] **Step 1: Write the failing test**

```python
"""Numbers to plain statuses (system health spec §4.3). Pure, so every boundary is a
one-line test."""

import pytest

from app.modules.monitoring import rules
from app.modules.monitoring.rules import Verdict

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("age", "status"), [(5, "ok"), (59.9, "ok"), (60, "bad"), (None, "bad")])
def test_heartbeat(age, status):
    assert rules.heartbeat(age).status == status


def test_a_missing_heartbeat_says_not_reporting():
    assert rules.heartbeat(None) == Verdict("bad", "not reporting")


@pytest.mark.parametrize(
    ("total", "failed", "status"),
    [
        (0, 0, "idle"),
        (4, 4, "ok"),      # under five runs: too few to judge
        (5, 0, "ok"),
        (100, 4, "ok"),
        (100, 5, "warn"),  # at or above 5 %
        (100, 20, "bad"),  # at or above 20 %
        (None, None, "unknown"),
    ],
)
def test_failure_share(total, failed, status):
    assert rules.failure_share(total, failed).status == status


def test_too_few_runs_still_names_the_failures():
    assert rules.failure_share(3, 1).reason == "1 of 3 failed (too few to judge)"


@pytest.mark.parametrize(
    ("key", "value", "status"),
    [
        ("outbox_age_s", 59, "ok"), ("outbox_age_s", 60, "warn"), ("outbox_age_s", 300, "bad"),
        ("consumer_lag", 100, "warn"), ("queue_ready", 1000, "bad"),
        ("dead_letters", 0, "ok"), ("dead_letters", 1, "warn"), ("dead_letters", 10, "bad"),
        ("first_token_p95_s", 4.9, "ok"), ("first_token_p95_s", 15, "bad"),
        ("retrieval_no_results_pct", 25, "warn"),
        ("outbox_age_s", None, "unknown"),
    ],
)
def test_levels(key, value, status):
    assert rules.level(key, value).status == status


def test_worst_of_picks_the_most_serious():
    assert rules.worst(["ok", "idle", "warn", "ok"]) == "warn"
    assert rules.worst(["ok", "bad", "unknown"]) == "bad"
    assert rules.worst([]) == "unknown"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_rules.py -q`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `app/modules/monitoring/rules.py`**

```python
"""From numbers to plain statuses (system health spec §4.3).

Every threshold is *at or above*: a value equal to `warn` is `warn`. The table is the one
the documentation quotes; change both together.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Status = Literal["ok", "warn", "bad", "idle", "unknown"]

#: A heartbeat older than this means the process is down.
HEARTBEAT_MAX_AGE_S = 60
#: A failure share needs this many runs before it can be warn or bad.
MIN_EVENTS = 5
FAILURE_PCT = (5.0, 20.0)

#: key -> (warn, bad, unit, what "ok" is called)
THRESHOLDS: dict[str, tuple[float, float, str, str]] = {
    "outbox_age_s": (60, 300, "s", "normal"),
    "consumer_lag": (100, 1000, "events", "keeping up"),
    "queue_ready": (100, 1000, "messages", "normal"),
    "dead_letters": (1, 10, "", "none"),
    "assistant_error_pct": (5, 20, "%", "normal"),
    "first_token_p95_s": (5, 15, "s", "fast"),
    "retrieval_no_results_pct": (25, 50, "%", "normal"),
}

_RANK = {"bad": 4, "warn": 3, "unknown": 2, "ok": 1, "idle": 0}


@dataclass(frozen=True)
class Verdict:
    status: Status
    reason: str


def heartbeat(age_seconds: float | None) -> Verdict:
    if age_seconds is None:
        return Verdict("bad", "not reporting")
    if age_seconds >= HEARTBEAT_MAX_AGE_S:
        return Verdict("bad", f"last seen {age_seconds / 60:.0f} min ago")
    return Verdict("ok", f"alive, seen {age_seconds:.0f} s ago")


def failure_share(total: float | None, failed: float | None) -> Verdict:
    if total is None or failed is None:
        return Verdict("unknown", "metrics unavailable")
    total, failed = int(total), int(failed)
    if total == 0:
        return Verdict("idle", "no runs in this window")
    if total < MIN_EVENTS:
        if failed:
            return Verdict("ok", f"{failed} of {total} failed (too few to judge)")
        return Verdict("ok", f"{total} runs, none failed")
    pct = failed / total * 100
    warn, bad = FAILURE_PCT
    status: Status = "bad" if pct >= bad else "warn" if pct >= warn else "ok"
    if not failed:
        return Verdict("ok", f"{total} runs, none failed")
    return Verdict(status, f"{failed} of {total} failed ({pct:.1f}%)")


def level(key: str, value: float | None) -> Verdict:
    warn, bad, unit, fine = THRESHOLDS[key]
    if value is None:
        return Verdict("unknown", "metrics unavailable")
    shown = f"{value:g}{' ' + unit if unit and unit != '%' else unit}"
    if value >= bad:
        return Verdict("bad", f"{shown}: far behind" if key != "dead_letters" else f"{shown} dead letters")
    if value >= warn:
        return Verdict("warn", f"{shown}: falling behind" if key != "dead_letters" else f"{shown} dead letters")
    return Verdict("ok", fine if value == 0 else f"{shown}: {fine}")


def worst(statuses: Iterable[Status]) -> Status:
    ranked = sorted(statuses, key=_RANK.__getitem__, reverse=True)
    return ranked[0] if ranked else "unknown"
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_rules.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/modules/monitoring/rules.py tests/tiers/t0_unit/test_monitoring_rules.py
git commit -m "feat(monitoring): status rules and thresholds"
```

---

### Task 7: Traces to failures

**Files:**
- Create: `app/modules/monitoring/traces.py`
- Test: `tests/tiers/t0_unit/test_monitoring_traces.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_traces.py -q`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `app/modules/monitoring/traces.py`**

```python
"""Jaeger trace JSON to a `Failure` (system health spec §3). Pure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Failure:
    trace_id: str
    at: datetime
    service: str
    operation: str
    failed_step: str
    message: str
    #: Relative: the browser opens it under the same host, behind the tools login.
    trace_path: str


def _tags(span: dict) -> dict:
    return {t["key"]: t["value"] for t in span.get("tags", [])}


def _is_error(span: dict) -> bool:
    return _tags(span).get("error") in (True, "true")


def _message(span: dict) -> str:
    for log in span.get("logs", []):
        fields = {f["key"]: f["value"] for f in log.get("fields", [])}
        if fields.get("exception.message"):
            return str(fields["exception.message"])
    tags = _tags(span)
    return str(tags.get("otel.status_description") or "no error message recorded")


def to_failure(trace: dict) -> Failure | None:
    spans = trace.get("spans") or []
    errors = sorted((s for s in spans if _is_error(s)), key=lambda s: s["startTime"])
    if not errors:
        return None
    root = min(
        (s for s in spans if not s.get("references")), key=lambda s: s["startTime"],
        default=min(spans, key=lambda s: s["startTime"]),
    )
    first = errors[0]
    processes = trace.get("processes") or {}
    service = processes.get(root.get("processID"), {}).get("serviceName", "unknown")
    return Failure(
        trace_id=trace["traceID"],
        at=datetime.fromtimestamp(root["startTime"] / 1_000_000, UTC),
        service=service,
        operation=root["operationName"],
        failed_step=first["operationName"],
        message=_message(first),
        trace_path=f"/jaeger/trace/{trace['traceID']}",
    )
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_traces.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/modules/monitoring/traces.py tests/tiers/t0_unit/test_monitoring_traces.py
git commit -m "feat(monitoring): reduce a Jaeger trace to its failed step and message"
```

---

### Task 8: Queries, schemas and the service

**Files:**
- Create: `app/modules/monitoring/queries.py`, `app/modules/monitoring/schemas.py`, `app/modules/monitoring/service.py`
- Test: `tests/tiers/t0_unit/test_monitoring_service.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_service.py -q`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `queries.py`**

```python
"""Every PromQL question the health page asks (system health spec §3, §4.1).

Names are as Prometheus sees them after the OTel collector: dots become underscores, a unit
suffix is added (`_milliseconds`, `_seconds`), counters end `_total`, and every series has
`job="smarthealth/<service.name>"`. The T3 test sends every query here to the real
Prometheus, so a typo fails there, not on the page.
"""

from __future__ import annotations

from dataclasses import dataclass

#: (job, what the page calls it). The heartbeat's `job` label is the process's service.name.
COMPONENTS: list[tuple[str, str]] = [
    ("smarthealth/part-a-api", "API"),
    ("smarthealth/part-a-worker", "Workflow worker"),
    ("smarthealth/part-a-relay", "Event relay"),
    ("smarthealth/part-a-consumers", "Event consumers"),
    ("smarthealth/part-a-celery", "Background jobs"),
    ("smarthealth/part-a-celery-beat", "Job scheduler"),
    ("smarthealth/knowledge", "Knowledge search"),
    ("smarthealth/assistant", "Assistant"),
]

HEARTBEAT_AGE = "time() - max by (job) (smarthealth_heartbeat_seconds)"
BROKER_UP = {
    "rabbitmq": 'max(up{job="rabbitmq"})',
    "kafka": "max(kafka_brokers)",
}


@dataclass(frozen=True)
class Flow:
    key: str
    label: str
    #: "workflow" (Temporal workflow_type), "task" (Celery task name), "search", "turn"
    kind: str
    name: str = ""


FLOWS: list[Flow] = [
    Flow("booking", "Booking", "workflow", "BookAppointmentWorkflow"),
    Flow("reschedule", "Reschedule", "workflow", "RescheduleWorkflow"),
    Flow("cancellation", "Cancellation follow-up", "workflow", "CancelFollowUpWorkflow"),
    Flow("billing", "Billing after a visit", "workflow", "PostVisitWorkflow"),
    Flow("provider_sync", "Provider deactivation", "workflow", "ProviderDeactivationWorkflow"),
    Flow("notifications", "Notifications and reminders", "task", "notifications.send"),
    Flow("documents", "Document text extraction", "task", "clinical.extract_document"),
    Flow("retrieval", "Knowledge search", "search"),
    Flow("assistant", "Assistant answers", "turn"),
]


def _sum(expr: str) -> str:
    return f"sum({expr}) or vector(0)"


def flow_total(flow: Flow, w: str, offset: str = "") -> str:
    off = f" offset {offset}" if offset else ""
    if flow.kind == "workflow":
        sel = f'{{workflow_type="{flow.name}"}}'
        return (f"({_sum(f'increase(temporal_workflow_completed_total{sel}[{w}]{off})')})"
                f" + ({_sum(f'increase(temporal_workflow_failed_total{sel}[{w}]{off})')})")
    if flow.kind == "task":
        return _sum(f'increase(smarthealth_celery_tasks_total{{task="{flow.name}",'
                    f'outcome=~"success|failure"}}[{w}]{off})')
    if flow.kind == "search":
        return _sum(f"increase(knowledge_searches_total[{w}]{off})")
    return _sum(f"increase(assistant_turns_total[{w}]{off})")


def flow_failed(flow: Flow, w: str) -> str:
    if flow.kind == "workflow":
        return _sum(f'increase(temporal_workflow_failed_total{{workflow_type="{flow.name}"}}[{w}])')
    if flow.kind == "task":
        return _sum(f'increase(smarthealth_celery_tasks_total{{task="{flow.name}",'
                    f'outcome="failure"}}[{w}])')
    if flow.kind == "search":
        return _sum(f'increase(knowledge_searches_total{{outcome="error"}}[{w}])')
    return _sum(f'increase(assistant_turns_total{{outcome="error"}}[{w}])')


def flow_p95_ms(flow: Flow, w: str) -> str | None:
    """p95 duration in ms where a histogram exists; Celery tasks have none."""
    if flow.kind == "workflow":
        return ("histogram_quantile(0.95, sum by (le) (increase("
                f'temporal_workflow_endtoend_latency_milliseconds_bucket{{workflow_type="{flow.name}"}}[{w}])))')
    if flow.kind == "search":
        return f"histogram_quantile(0.95, sum by (le) (increase(knowledge_search_duration_milliseconds_bucket[{w}])))"
    if flow.kind == "turn":
        return f"histogram_quantile(0.95, sum by (le) (increase(assistant_turn_duration_milliseconds_bucket[{w}])))"
    return None


def backlogs(w: str) -> list[tuple[str, str, str, str]]:
    """(key, label, rule key, PromQL)."""
    return [
        ("outbox", "Oldest unpublished event", "outbox_age_s",
         "max(smarthealth_outbox_oldest_unpublished_age_seconds)"),
        ("consumer_lag", "Largest event consumer lag", "consumer_lag",
         "max(sum by (consumergroup) (kafka_consumergroup_lag))"),
        ("queue_ready", "Largest job queue", "queue_ready",
         r'max(rabbitmq_queue_messages_ready{queue!~".*\\.dlq"})'),
        ("event_dead_letters", "Events dead-lettered", "dead_letters",
         _sum(f'increase(smarthealth_consumer_events_total{{outcome="dead_lettered"}}[{w}])')),
        ("job_dead_letters", "Notifications dead-lettered", "dead_letters",
         _sum(r'rabbitmq_queue_messages_ready{queue=~".*\\.dlq"}')),
    ]


def ai(w: str) -> dict[str, str]:
    return {
        "turns": _sum(f"increase(assistant_turns_total[{w}])"),
        "errors": _sum(f'increase(assistant_turns_total{{outcome="error"}}[{w}])'),
        "first_token_p95_ms": ("histogram_quantile(0.95, sum by (le) "
                               f"(increase(assistant_first_token_milliseconds_bucket[{w}])))"),
        "searches": _sum(f"increase(knowledge_searches_total[{w}])"),
        "no_results": _sum(f'increase(knowledge_searches_total{{outcome="no_results"}}[{w}])'),
        "tokens": _sum(f"increase(assistant_llm_tokens_total[{w}])"),
    }


def every_query(w: str = "24h") -> list[str]:
    """All of the above, for the T3 test that sends each to a real Prometheus."""
    out = [HEARTBEAT_AGE, *BROKER_UP.values()]
    for flow in FLOWS:
        out += [flow_total(flow, w), flow_total(flow, w, w), flow_failed(flow, w)]
        if p95 := flow_p95_ms(flow, w):
            out.append(p95)
    out += [q for *_, q in backlogs(w)]
    out += list(ai(w).values())
    return out
```

- [ ] **Step 4: Implement `schemas.py`**

```python
"""Response models for GET /monitoring/* (system health spec §4.1, §4.2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["ok", "warn", "bad", "idle", "unknown"]
SourceStatus = Literal["ok", "unavailable", "not_configured"]


class Sources(BaseModel):
    prometheus: SourceStatus
    jaeger: SourceStatus


class Component(BaseModel):
    name: str
    job: str
    status: Status
    reason: str


class FlowRead(BaseModel):
    key: str
    label: str
    total: int | None
    failed: int | None
    failure_pct: float | None
    p95_seconds: float | None
    previous_total: int | None
    status: Status
    reason: str


class Backlog(BaseModel):
    key: str
    label: str
    value: float | None
    status: Status
    reason: str


class AiHealth(BaseModel):
    turns: int | None
    errors: int | None
    error_pct: float | None
    first_token_p95_seconds: float | None
    retrieval_no_results_pct: float | None
    tokens: int | None
    status: Status
    reason: str


class Overview(BaseModel):
    window_hours: int
    generated_at: datetime
    sources: Sources
    components: list[Component]
    flows: list[FlowRead]
    backlogs: list[Backlog]
    ai: AiHealth


class FailureRead(BaseModel):
    trace_id: str
    at: datetime
    service: str
    operation: str
    failed_step: str
    message: str
    trace_path: str


class FailuresRead(BaseModel):
    window_hours: int
    source: SourceStatus
    failures: list[FailureRead]
```

- [ ] **Step 5: Implement `service.py`**

```python
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
        answers = dict(zip(range(len(asked)), await asyncio.gather(*(ask(q) for q in asked)),
                           strict=True))
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
                            jaeger="ok" if self._jaeger is not None else "not_configured"),
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
            failure = to_failure(trace)
            if failure is not None and failure.trace_id not in seen:
                seen[failure.trace_id] = FailureRead(**asdict(failure))
        ordered = sorted(seen.values(), key=lambda f: f.at, reverse=True)[:limit]
        return self._store(("failures", hours, limit),
                           FailuresRead(window_hours=hours, source="ok", failures=ordered))
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/tiers/t0_unit/test_monitoring_service.py -q`
Expected: all pass. If `test_a_healthy_system...` fails on a broker tile, check that both
`BROKER_UP` answers are in the fake. Every component, brokers included, must be `ok`.

- [ ] **Step 7: Commit**

```bash
git add app/modules/monitoring tests/tiers/t0_unit/test_monitoring_service.py
git commit -m "feat(monitoring): the overview and failures service, with a 15 s cache"
```

---

### Task 9: Router, wiring, contract tests, and the validated case

**Files:**
- Create: `app/modules/monitoring/router.py`
- Modify: `app/api/router.py`, `app/main.py`
- Test: `tests/tiers/t1_contract/test_monitoring_contract.py`
- Create: `tests/cases/pa-monitoring-001-admin-sees-system-health.yaml`

- [ ] **Step 1: Write the failing contract test**

```python
"""GET /monitoring/*: admin only, and a dead source is a 200 that says so."""

import uuid

import pytest

from app.main import app
from app.modules.identity.models import UserRole
from app.modules.monitoring.router import get_monitoring
from app.modules.monitoring.service import MonitoringService
from tests.harness.auth import bearer, issue_token
from tests.tiers.t0_unit.test_monitoring_service import FakeJaeger, FakePrometheus

pytestmark = pytest.mark.contract


def _as(role: UserRole) -> dict[str, str]:
    return bearer(issue_token(subject=str(uuid.uuid4()), role=role))


@pytest.fixture
def monitoring():
    service = MonitoringService(FakePrometheus(down=True), FakeJaeger())
    app.dependency_overrides[get_monitoring] = lambda: service
    yield service
    app.dependency_overrides.pop(get_monitoring, None)


@pytest.mark.parametrize("path", ["/monitoring/overview", "/monitoring/failures"])
async def test_the_health_page_needs_a_token(api_client, monitoring, path):
    assert (await api_client.get(path)).status_code == 401


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK])
@pytest.mark.parametrize("path", ["/monitoring/overview", "/monitoring/failures"])
async def test_only_admins_see_system_health(api_client, monitoring, role, path):
    assert (await api_client.get(path, headers=_as(role))).status_code == 403


async def test_prometheus_down_is_a_200_that_says_so(api_client, monitoring):
    response = await api_client.get("/monitoring/overview?hours=24", headers=_as(UserRole.ADMIN))
    assert response.status_code == 200
    body = response.json()
    assert body["sources"]["prometheus"] == "unavailable"
    assert {f["status"] for f in body["flows"]} == {"unknown"}


async def test_failures_answer_with_their_source(api_client, monitoring):
    response = await api_client.get("/monitoring/failures?hours=1&limit=5",
                                    headers=_as(UserRole.ADMIN))
    assert response.status_code == 200
    assert response.json() == {"window_hours": 1, "source": "ok", "failures": []}


@pytest.mark.parametrize("query", ["hours=2", "hours=0", "limit=0", "limit=51"])
async def test_windows_and_limits_outside_the_allowed_set_are_refused(api_client, monitoring, query):
    path = "/monitoring/failures" if "limit" in query else "/monitoring/overview"
    response = await api_client.get(f"{path}?{query}", headers=_as(UserRole.ADMIN))
    assert response.status_code == 422
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/tiers/t1_contract/test_monitoring_contract.py -q`
Expected: FAIL, `ImportError: app.modules.monitoring.router`.

- [ ] **Step 3: Implement `router.py`**

```python
"""GET /monitoring/overview and GET /monitoring/failures (system health spec §4).

Admin only. Answers 200 even when Prometheus or Jaeger is down: each card and the
`sources` block say which, so the page can show what it still knows.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.monitoring.schemas import FailuresRead, Overview
from app.modules.monitoring.service import MonitoringService

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
Hours = Literal[1, 24, 168]


def get_monitoring(request: Request) -> MonitoringService:
    return request.app.state.monitoring


@router.get("/overview", response_model=Overview)
async def overview(
    hours: Hours = 24,
    monitoring: MonitoringService = Depends(get_monitoring),
    _: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> Overview:
    """Every process's liveness, the critical flows, backlogs and AI health over the last
    `hours` (1, 24 or 168), in plain statuses with a reason each."""
    return await monitoring.overview(hours)


@router.get("/failures", response_model=FailuresRead)
async def failures(
    hours: Hours = 24,
    limit: int = Query(default=20, ge=1, le=50),
    monitoring: MonitoringService = Depends(get_monitoring),
    _: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> FailuresRead:
    """The newest error traces across the services, each reduced to its root operation,
    first failed step and message, with a link to the trace."""
    return await monitoring.failures(hours, limit)
```

Note `Hours = Literal[1, 24, 168]` as a query type. FastAPI validates `hours=2` as 422. If
this FastAPI version refuses an int `Literal` for a query, use
`hours: int = Query(default=24)` plus an explicit check that raises `app.core.errors.Unprocessable`.

In `app/api/router.py` add `from app.modules.monitoring import router as monitoring_router`
(alphabetical, after identity) and `api_router.include_router(monitoring_router.router)`
(after `identity_router.users_router`).

In `app/main.py` add `from app.modules.monitoring.service import MonitoringService` to the
imports. In `lifespan`, after `app.state.documents = ...`, add
`app.state.monitoring = MonitoringService.from_settings(settings)`. In the `finally:` block,
after `app.state.telemetry.flush()`, add `await app.state.monitoring.aclose()`.

- [ ] **Step 4: Run the contract tests and the meta-tests**

Run: `python -m pytest tests/tiers/t1_contract/test_monitoring_contract.py tests/meta/test_import_boundaries.py tests/tiers/t1_contract/test_openapi_security.py -q`
Expected: all pass. `test_openapi_security` should pick up the two new secured routes; if it
keeps an explicit list of routes, add them there.

- [ ] **Step 5: Author the validated case**

Use the smarthealth-testcase skill with: "An admin reads GET /monitoring/overview and
/monitoring/failures and gets 200 with per-source status even when Prometheus is down;
patients, providers and front desk get 403." Contract tier, requirement traces
`PART-A-FR-6` (observability) and the Week 5 AI monitoring metrics. Reference the tests above.
Then run: `python -m tests.runner.route_check`
Expected: the new case validates and routes to the contract tier.

- [ ] **Step 6: Commit**

```bash
git add app/modules/monitoring/router.py app/api/router.py app/main.py tests/tiers/t1_contract/test_monitoring_contract.py tests/cases/pa-monitoring-001-*.yaml
git commit -m "feat(monitoring): admin-only GET /monitoring/overview and /monitoring/failures"
```

---

### Task 10: Configuration in compose and the charts

**Files:** Modify: `docker-compose.infra.yml`, `deploy/helm/charts/core/values.yaml`

- [ ] **Step 1: Compose.** In the `app` service's `environment`, beside
  `SMARTHEALTH_OTEL_ENDPOINT_HTTP`, add:

```yaml
      SMARTHEALTH_PROMETHEUS_URL: http://prometheus:9090
      SMARTHEALTH_JAEGER_QUERY_URL: http://jaeger:16686
```

- [ ] **Step 2: Chart.** In `deploy/helm/charts/core/values.yaml`, beside
  `SMARTHEALTH_OTEL_ENDPOINT_HTTP` (line 12), add:

```yaml
  SMARTHEALTH_PROMETHEUS_URL: http://prometheus:9090/prometheus
  SMARTHEALTH_JAEGER_QUERY_URL: http://jaeger:16686/jaeger
```

Check Prometheus's path prefix in the chart:
`grep -rn "route-prefix\|external-url\|web.route" deploy/helm/charts/prometheus`.
- If Prometheus serves under `/prometheus` internally, keep the `/prometheus` suffix.
- If the ingress strips it (the `strip-prometheus` middleware suggests Prometheus serves at
  `/`), use `http://prometheus:9090` with no suffix.

Check the service names the same way:
`grep -rn "name:" deploy/helm/charts/prometheus/templates/*.yaml deploy/helm/charts/jaeger/templates/*.yaml | grep -i service -A1`.

- [ ] **Step 3: Chart tests.** Run: `python -m pytest tests/meta/test_helm_charts.py -q`
  Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.infra.yml deploy/helm/charts/core/values.yaml
git commit -m "build: point the API at Prometheus and Jaeger for the health page"
```

---

### Task 11: The `system_health` report kind

**Files:**
- Modify: `services/assistant/assistant_service/parta.py`, `services/assistant/assistant_service/reports.py`, `tests/harness/fake_part_a.py`
- Test: `tests/tiers/t1_contract/test_assistant_agent.py`

- [ ] **Step 1: Add the endpoints to the fake.** In `FakePartA.handle`, before the
  `/analytics/timeseries` branch:

```python
        if (method, path) == ("GET", "/monitoring/overview"):
            return httpx.Response(200, json=self.monitoring_overview)
        if (method, path) == ("GET", "/monitoring/failures"):
            return httpx.Response(200, json=self.monitoring_failures)
```

And add these two fields to the dataclass, after `outcome`:

```python
    #: GET /monitoring/overview and /monitoring/failures (system health report).
    monitoring_overview: dict = field(default_factory=lambda: {
        "window_hours": 24, "generated_at": "2026-11-17T10:00:00Z",
        "sources": {"prometheus": "ok", "jaeger": "ok"},
        "components": [
            {"name": "API", "job": "smarthealth/part-a-api", "status": "ok",
             "reason": "alive, seen 4 s ago"},
            {"name": "Background jobs", "job": "smarthealth/part-a-celery", "status": "bad",
             "reason": "not reporting"},
        ],
        "flows": [
            {"key": "booking", "label": "Booking", "total": 40, "failed": 9,
             "failure_pct": 22.5, "p95_seconds": 1.8, "previous_total": 35, "status": "bad",
             "reason": "9 of 40 failed (22.5%)"},
        ],
        "backlogs": [
            {"key": "queue_ready", "label": "Largest job queue", "value": 240.0,
             "status": "warn", "reason": "240 messages: falling behind"},
        ],
        "ai": {"turns": 50, "errors": 1, "error_pct": 2.0, "first_token_p95_seconds": 1.1,
               "retrieval_no_results_pct": 8.0, "tokens": 9000, "status": "ok",
               "reason": "1 of 50 failed (2.0%)"},
    })
    monitoring_failures: dict = field(default_factory=lambda: {
        "window_hours": 24, "source": "ok", "failures": [
            {"trace_id": "t1", "at": "2026-11-17T09:58:00Z", "service": "part-a-worker",
             "operation": "RunWorkflow:BookAppointmentWorkflow",
             "failed_step": "RunActivity:billing.precheck",
             "message": "gateway unavailable (chaos)", "trace_path": "/jaeger/trace/t1"},
        ]})
```

- [ ] **Step 2: Write the failing tests** (append to `test_assistant_agent.py`, after the
  utilisation test)

```python
async def test_a_system_health_report_states_the_problems_from_part_a_offline():
    """System health spec §6: the cards carry Part A's figures; with no model the
    narrative is a deterministic template naming what is wrong."""
    async with Harness(FakePartA.seeded()) as h:
        admin = _token(UserRole.ADMIN, user="u-admin")
        job = await h.stub.StartReport(
            assistant_pb2.StartReportRequest(kind="system_health"), metadata=_auth(admin)
        )
        events = await _watch(h, admin, job.report_id)
    assert job.title == "System health snapshot"
    cards = [e.card for e in events if e.WhichOneof("kind") == "card"]
    attention = {f.label: f.value for f in cards[0].fields}
    assert attention["Background jobs"] == "not reporting"
    assert attention["Booking"] == "9 of 40 failed (22.5%)"
    assert attention["Largest job queue"] == "240 messages: falling behind"
    failures = {f.label: f.value for f in cards[1].fields}
    assert "RunActivity:billing.precheck" in next(iter(failures.values()))
    body = text(events)
    assert "Needs attention" in body and "Background jobs" in body
    assert events[-1].done.reason == "complete"


async def test_a_system_health_report_lets_the_model_phrase_the_same_figures():
    narrative = "Booking is failing at the billing pre-check; the Celery worker is down."
    model = Scripted(script=[AIMessage(content=narrative)])
    async with Harness(FakePartA.seeded(), model=model) as h:
        admin = _token(UserRole.ADMIN, user="u-admin")
        job = await h.stub.StartReport(
            assistant_pb2.StartReportRequest(kind="system_health"), metadata=_auth(admin)
        )
        events = await _watch(h, admin, job.report_id)
    assert narrative in text(events)
    # The model was given the figures, not asked to find them.
    prompt = model.seen[-1][-1].content
    assert "9 of 40 failed" in prompt and "gateway unavailable" in prompt
```

Check how `Scripted` exposes what it was sent:
`grep -n "class Scripted" -A 30 tests/tiers/t1_contract/test_assistant_agent.py`. If the
attribute isn't `seen`, use the one it has. If it records nothing, add
`self.seen.append(messages)` to its `astream`/`ainvoke` with `seen: list = field(default_factory=list)`.

- [ ] **Step 3: Run them to make sure they fail**

Run: `python -m pytest tests/tiers/t1_contract/test_assistant_agent.py -k system_health -q`
Expected: FAIL, `INVALID_ARGUMENT` (`kind is one of …`).

- [ ] **Step 4: Implement**

In `parta.py`, after `analytics_series`:

```python
    async def monitoring_overview(self, token: str, *, hours: int = 24) -> dict:
        """Part A's health page figures (system health spec §4.1). Admin only."""
        return await self.request("GET", "/monitoring/overview", token,
                                  params={"hours": hours})

    async def monitoring_failures(self, token: str, *, hours: int = 24, limit: int = 10) -> dict:
        return await self.request("GET", "/monitoring/failures", token,
                                  params={"hours": hours, "limit": limit})
```

In `reports.py`:

1. Add to `KINDS`: `"system_health": ("System health snapshot", 1),`.
2. Add, next to the other instruction constants:

```python
HEALTH_INSTRUCTIONS = (
    "You write a system health snapshot for the administrator of MediNova's scheduling "
    "platform, who is not an engineer. You are given the health page's figures: each "
    "component, flow and backlog with a status (ok, warn, bad, idle, unknown) and a reason, "
    "and the most recent failures with the step that failed and its error message. Write: "
    "one sentence on overall health; then 'Needs attention' with each warn or bad item and "
    "its likely cause, using the failed steps; then 'Healthy' in one line; then 2-3 "
    "concrete things to check next. Use only the figures given: never invent a number, "
    "service or cause. Plain words; no PromQL."
)
```

3. At the top of `_write`, before the utilisation branch:

```python
        if report.kind == "system_health":
            await self._write_health(caller, emit, usage)
            return
```

4. The writer:

```python
    async def _write_health(self, caller: Caller, emit, usage: TurnUsage) -> None:
        """System health snapshot (system health spec §6): Part A's health page, read with
        the admin's own token, then explained. Every figure comes from Part A."""
        call = "health-1"
        tool = {"kind": "tool", "call_id": call, "name": "monitoring",
                "label": "Reading the health page"}
        await emit({**tool, "status": "running", "summary": ""})
        overview, failures = await asyncio.gather(
            self._parta.monitoring_overview(caller.token, hours=24),
            self._parta.monitoring_failures(caller.token, hours=24, limit=10),
        )
        items = (
            [(c["name"], c["status"], c["reason"]) for c in overview["components"]]
            + [(f["label"], f["status"], f["reason"]) for f in overview["flows"]]
            + [(b["label"], b["status"], b["reason"]) for b in overview["backlogs"]]
            + [("Assistant", overview["ai"]["status"], overview["ai"]["reason"])]
        )
        attention = [i for i in items if i[1] in ("bad", "warn")]
        attention.sort(key=lambda i: i[1] != "bad")  # bad first
        await emit({**tool, "status": "done",
                    "summary": f"{len(attention)} need attention, "
                               f"{len(failures['failures'])} recent failures"})
        unseen = [k for k, v in overview["sources"].items() if v != "ok"]
        await emit({
            "kind": "card", "card_kind": "metrics", "title": "Needs attention",
            "subtitle": ("Last 24 hours" if not unseen
                         else f"Last 24 hours — not visible: {', '.join(unseen)}"),
            "fields": [{"label": n, "value": r} for n, _s, r in attention]
            or [{"label": "Everything", "value": "healthy"}],
            "badge": f"{len(attention)}", "link": "/health",
        })
        recent = failures["failures"][:5]
        if recent:
            await emit({
                "kind": "card", "card_kind": "metrics", "title": "Recent failures",
                "subtitle": "Newest first; the trace link needs the tools login",
                "fields": [{"label": f"{f['at'][11:16]} {f['service']}",
                            "value": f"{f['failed_step']}: {f['message']}"} for f in recent],
                "badge": f"{len(failures['failures'])}", "link": "/health",
            })
        material = "Statuses (name | status | reason):\n" + "\n".join(
            f"{n} | {s} | {r}" for n, s, r in items
        ) + "\nRecent failures (service | operation | failed step | message):\n" + "\n".join(
            f"{f['service']} | {f['operation']} | {f['failed_step']} | {f['message']}"
            for f in recent
        )
        if unseen:
            material += f"\nNot visible (source down): {', '.join(unseen)}"
        await self._narrative(emit, usage, material, attention, recent, kind="health")
```

5. In `_narrative`, add `"health": HEALTH_INSTRUCTIONS,` to the instructions dict. Before
   the `if kind == "engagement":` fallback, add the offline template. Note: `figures` is
   `attention` and `busiest` is `recent` for this kind.

```python
        if kind == "health":
            attention, recent = figures, busiest
            lines = (["**Needs attention**", ""]
                     + [f"- **{n}**: {r}" for n, _s, r in attention]
                     if attention else ["Everything the health page watches is healthy."])
            if recent:
                lines += ["", "**Most recent failure**", "",
                          f"- {recent[0]['operation']} failed at "
                          f"{recent[0]['failed_step']}: {recent[0]['message']}"]
            text = "\n".join(lines) + "\n"
            for word in text.split(" "):
                await emit({"kind": "token", "text": word + " "})
            return
```

- [ ] **Step 5: Run the assistant tests**

Run: `python -m pytest tests/tiers/t1_contract/test_assistant_agent.py tests/meta/test_service_independence.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add services/assistant tests/harness/fake_part_a.py tests/tiers/t1_contract/test_assistant_agent.py
git commit -m "feat(assistant): System health snapshot report from Part A's health page"
```

---

### Task 12: The frontend page

**Files:**
- Create: `frontend/src/health.ts`, `frontend/src/health.test.ts`, `frontend/src/pages/Health.tsx`
- Modify: `frontend/src/main.tsx`, `frontend/src/pages/Reports.tsx`

- [ ] **Step 1: Write the failing test** `frontend/src/health.test.ts`

```ts
import { describe, expect, it } from "vitest";
import { overallStatus, statusLabel, trend, unseenSources } from "./health";

describe("health helpers", () => {
  it("names each status in plain words", () => {
    expect(statusLabel("ok")).toBe("Healthy");
    expect(statusLabel("warn")).toBe("Needs a look");
    expect(statusLabel("bad")).toBe("Failing");
    expect(statusLabel("idle")).toBe("No activity");
    expect(statusLabel("unknown")).toBe("Can't tell");
  });

  it("overall status is the worst one", () => {
    expect(overallStatus(["ok", "idle", "warn"])).toBe("warn");
    expect(overallStatus(["ok", "bad", "unknown"])).toBe("bad");
    expect(overallStatus([])).toBe("unknown");
  });

  it("trend compares with the window before", () => {
    expect(trend(40, 35)).toBe("up");
    expect(trend(30, 35)).toBe("down");
    expect(trend(35, 35)).toBe("same");
    expect(trend(null, 35)).toBe("same");
  });

  it("lists the sources it cannot see", () => {
    expect(unseenSources({ prometheus: "unavailable", jaeger: "ok" })).toEqual(["metrics (Prometheus)"]);
    expect(unseenSources({ prometheus: "ok", jaeger: "not_configured" })).toEqual(["traces (Jaeger)"]);
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd frontend && npx vitest run src/health.test.ts`
Expected: FAIL, cannot resolve `./health`.

- [ ] **Step 3: Implement `frontend/src/health.ts`**

```ts
// Types for GET /monitoring/* and the pure helpers the System health page uses.

export type Status = "ok" | "warn" | "bad" | "idle" | "unknown";
export type SourceStatus = "ok" | "unavailable" | "not_configured";

export interface Component { name: string; job: string; status: Status; reason: string }
export interface Flow {
  key: string; label: string; total: number | null; failed: number | null;
  failure_pct: number | null; p95_seconds: number | null; previous_total: number | null;
  status: Status; reason: string;
}
export interface Backlog { key: string; label: string; value: number | null; status: Status; reason: string }
export interface AiHealth {
  turns: number | null; errors: number | null; error_pct: number | null;
  first_token_p95_seconds: number | null; retrieval_no_results_pct: number | null;
  tokens: number | null; status: Status; reason: string;
}
export interface Overview {
  window_hours: number; generated_at: string;
  sources: { prometheus: SourceStatus; jaeger: SourceStatus };
  components: Component[]; flows: Flow[]; backlogs: Backlog[]; ai: AiHealth;
}
export interface Failure {
  trace_id: string; at: string; service: string; operation: string;
  failed_step: string; message: string; trace_path: string;
}
export interface Failures { window_hours: number; source: SourceStatus; failures: Failure[] }

const LABEL: Record<Status, string> = {
  ok: "Healthy", warn: "Needs a look", bad: "Failing", idle: "No activity", unknown: "Can't tell",
};
const RANK: Record<Status, number> = { bad: 4, warn: 3, unknown: 2, ok: 1, idle: 0 };

export function statusLabel(status: Status): string {
  return LABEL[status];
}

export function overallStatus(statuses: Status[]): Status {
  return statuses.reduce<Status | null>((worst, s) => (worst === null || RANK[s] > RANK[worst] ? s : worst), null) ?? "unknown";
}

export function trend(now: number | null, before: number | null): "up" | "down" | "same" {
  if (now === null || before === null || now === before) return "same";
  return now > before ? "up" : "down";
}

export function unseenSources(sources: Overview["sources"]): string[] {
  const out: string[] = [];
  if (sources.prometheus !== "ok") out.push("metrics (Prometheus)");
  if (sources.jaeger !== "ok") out.push("traces (Jaeger)");
  return out;
}

/** Tailwind classes per status: dot colour and badge tone. */
export const TONE: Record<Status, string> = {
  ok: "bg-emerald-500", warn: "bg-amber-500", bad: "bg-red-500", idle: "bg-slate-400", unknown: "bg-slate-300",
};
```

- [ ] **Step 4: Run it**

Run: `cd frontend && npx vitest run src/health.test.ts`
Expected: 4 passed.

- [ ] **Step 5: Implement `frontend/src/pages/Health.tsx`**

```tsx
// System health, for admin: is everything running, what failed recently and why, and how
// the AI is doing -- Part A's GET /monitoring/* in plain words (system health spec §5).

import { useQuery } from "@tanstack/react-query";
import { ArrowDownIcon, ArrowUpIcon, FileTextIcon, MinusIcon } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/api";
import { assistant } from "@/assistant/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  type Failures, type Overview, type Status, overallStatus, statusLabel, TONE, trend, unseenSources,
} from "@/health";

const RANGES = [
  { hours: 1, label: "1 hour" },
  { hours: 6, label: "6 hours" },
  { hours: 24, label: "24 hours" },
];

function Dot({ status }: { status: Status }) {
  return <span className={`inline-block size-2.5 shrink-0 rounded-full ${TONE[status]}`} aria-hidden />;
}

function Pill({ status }: { status: Status }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium">
      <Dot status={status} /> {statusLabel(status)}
    </span>
  );
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export default function Health() {
  const [hours, setHours] = useState(24);
  const [starting, setStarting] = useState(false);
  const navigate = useNavigate();
  const overview = useQuery({
    queryKey: ["monitoring", "overview", hours],
    queryFn: () => api<Overview>(`/monitoring/overview?hours=${hours}`),
    refetchInterval: 30_000,
  });
  const failures = useQuery({
    queryKey: ["monitoring", "failures", hours],
    queryFn: () => api<Failures>(`/monitoring/failures?hours=${hours}&limit=20`),
    refetchInterval: 30_000,
  });
  const data = overview.data;

  async function writeReport() {
    setStarting(true);
    try {
      await assistant.startReport({ kind: "system_health" });
      navigate("/reports");
    } finally {
      setStarting(false);
    }
  }

  const overall = data
    ? overallStatus([...data.components, ...data.flows, ...data.backlogs, data.ai].map((x) => x.status))
    : "unknown";
  const unseen = data ? unseenSources(data.sources) : [];

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <h1 className="flex items-center gap-2 text-lg font-semibold">
            System health {data && <Pill status={overall} />}
          </h1>
          <p className="text-sm text-muted-foreground">
            Every part of the platform, its critical flows and recent failures — refreshed every 30 seconds
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {RANGES.map((r) => (
            <Button key={r.hours} size="sm" variant={r.hours === hours ? "default" : "outline"} onClick={() => setHours(r.hours)}>
              {r.label}
            </Button>
          ))}
          <Button size="sm" variant="secondary" onClick={() => void writeReport()} disabled={starting}>
            {starting ? <Spinner /> : <FileTextIcon />} Write a health report
          </Button>
        </div>
      </div>

      {overview.isPending && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Checking…</div>
      )}
      {overview.isError && <p className="text-sm text-destructive">Couldn't load health: {overview.error.message}</p>}
      {unseen.length > 0 && (
        <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          Can't see {unseen.join(" or ")} right now — those cards say "Can't tell" until it is back.
        </p>
      )}

      {data && (
        <>
          <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            {data.components.map((c) => (
              <Card key={c.job} className="gap-1 py-3">
                <CardContent className="flex flex-col gap-1 px-3">
                  <span className="flex items-center gap-2 text-sm font-medium"><Dot status={c.status} /> {c.name}</span>
                  <span className="text-xs text-muted-foreground">{c.reason}</span>
                </CardContent>
              </Card>
            ))}
          </section>

          <Card>
            <CardHeader>
              <CardTitle>Critical flows</CardTitle>
              <CardDescription>Runs in the last {RANGES.find((r) => r.hours === hours)!.label}, against the window before</CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Flow</TableHead><TableHead className="text-right">Runs</TableHead>
                    <TableHead className="text-right">Failed</TableHead><TableHead className="text-right">Slowest 5% take</TableHead>
                    <TableHead>Status</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.flows.map((f) => {
                    const t = trend(f.total, f.previous_total);
                    return (
                      <TableRow key={f.key}>
                        <TableCell className="font-medium">{f.label}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          <span className="inline-flex items-center gap-1">
                            {f.total ?? "—"}
                            {t === "up" ? <ArrowUpIcon className="size-3" /> : t === "down" ? <ArrowDownIcon className="size-3" /> : <MinusIcon className="size-3 opacity-40" />}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{f.failed ?? "—"}{f.failure_pct !== null && f.failed ? ` (${f.failure_pct}%)` : ""}</TableCell>
                        <TableCell className="text-right tabular-nums">{f.p95_seconds !== null ? `${f.p95_seconds} s` : "—"}</TableCell>
                        <TableCell><Pill status={f.status} /> <span className="text-xs text-muted-foreground">{f.reason}</span></TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent failures</CardTitle>
              <CardDescription>Newest first, from the traces. "View trace" opens Jaeger (tools login).</CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              {failures.data?.source !== "ok" ? (
                <p className="text-sm text-muted-foreground">Traces are not visible right now.</p>
              ) : failures.data.failures.length === 0 ? (
                <p className="text-sm text-muted-foreground">No failures in this window.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow><TableHead>When</TableHead><TableHead>Where</TableHead><TableHead>What failed</TableHead><TableHead /></TableRow>
                  </TableHeader>
                  <TableBody>
                    {failures.data.failures.map((f) => (
                      <TableRow key={f.trace_id}>
                        <TableCell className="whitespace-nowrap">{when(f.at)}</TableCell>
                        <TableCell><div className="font-medium">{f.operation}</div><div className="text-xs text-muted-foreground">{f.service}</div></TableCell>
                        <TableCell><div>{f.failed_step}</div><div className="text-xs text-destructive">{f.message}</div></TableCell>
                        <TableCell><a className="text-sm underline" href={f.trace_path} target="_blank" rel="noreferrer">View trace</a></TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <section className="grid gap-3 lg:grid-cols-2">
            <Card>
              <CardHeader><CardTitle>Backlogs</CardTitle><CardDescription>Work waiting to be done</CardDescription></CardHeader>
              <CardContent className="flex flex-col gap-2">
                {data.backlogs.map((b) => (
                  <div key={b.key} className="flex items-center justify-between gap-3 text-sm">
                    <span className="flex items-center gap-2"><Dot status={b.status} /> {b.label}</span>
                    <span className="text-muted-foreground">{b.reason}</span>
                  </div>
                ))}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">AI health <Pill status={data.ai.status} /></CardTitle>
                <CardDescription>{data.ai.reason} · <a className="underline" href="/usage">AI usage</a></CardDescription>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-3 text-sm">
                <div><div className="text-muted-foreground">Answers</div><div className="text-lg font-semibold">{data.ai.turns ?? "—"}</div></div>
                <div><div className="text-muted-foreground">Errors</div><div className="text-lg font-semibold">{data.ai.error_pct !== null ? `${data.ai.error_pct}%` : "—"}</div></div>
                <div><div className="text-muted-foreground">First word (slowest 5%)</div><div className="text-lg font-semibold">{data.ai.first_token_p95_seconds !== null ? `${data.ai.first_token_p95_seconds} s` : "—"}</div></div>
                <div><div className="text-muted-foreground">Searches with no result</div><div className="text-lg font-semibold">{data.ai.retrieval_no_results_pct !== null ? `${data.ai.retrieval_no_results_pct}%` : "—"}</div></div>
              </CardContent>
            </Card>
          </section>
          <p className="text-xs text-muted-foreground">Checked {when(data.generated_at)}. Business figures are on Reports; engineering detail is in Grafana.</p>
        </>
      )}
    </div>
  );
}
```

Check `react-router-dom` is the router package and that the `assistant` client exposes
`startReport` as `Reports.tsx` uses it: `grep -n "react-router\|startReport" frontend/src/pages/Reports.tsx frontend/src/main.tsx`.

- [ ] **Step 6: Wire it in.** In `frontend/src/main.tsx`:
  - Add `import Health from "./pages/Health";`.
  - Add `{ to: "/health", label: "System health", roles: ["admin"] },` after the AI usage nav entry.
  - Add `<Route path="/health" element={<Health />} />` after the `/usage` route.

  In `Reports.tsx`, add `["system_health", "System health"],` to the kinds list at line 83.

- [ ] **Step 7: Type-check, test and build**

Run: `cd frontend && npx tsc --noEmit && npm test && npm run build`
Expected: no type errors, all Vitest suites pass, build succeeds.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/health.ts frontend/src/health.test.ts frontend/src/pages/Health.tsx frontend/src/main.tsx frontend/src/pages/Reports.tsx
git commit -m "feat(frontend): System health page for admin"
```

---

### Task 13: Grafana panels and the T3 test against real sources

**Files:**
- Modify: `ops/grafana/dashboards/async-backbone.json`, `assistant.json`, `service-overview.json`
- Create: `tests/tiers/t3_integration/test_monitoring_sources.py`

- [ ] **Step 1: Grafana panels.** Each dashboard is JSON with a `panels` list. Copy an existing
  timeseries panel in the same file and change its `title`, its `targets[0].expr` and its
  `gridPos.y` (to the bottom: the max `y + h` in the file). Give each panel a unique `id`
  (max id + 1).

| Dashboard | Title | expr |
| --- | --- | --- |
| async-backbone | Celery task outcomes | `sum by (task, outcome) (rate(smarthealth_celery_tasks_total[$__rate_interval]))` |
| assistant | Knowledge searches by outcome | `sum by (outcome) (rate(knowledge_searches_total[$__rate_interval]))` |
| assistant | Knowledge search p95 (ms) | `histogram_quantile(0.95, sum by (le) (rate(knowledge_search_duration_milliseconds_bucket[$__rate_interval])))` |
| service-overview | Seconds since each process was last seen | `time() - max by (job) (smarthealth_heartbeat_seconds)` (a table or bar-gauge panel copied from any stat panel; unit `s`) |

Then run `python scripts/sync_chart_files.py` and
`python -m pytest tests/meta -q -k "grafana or chart"`.
Expected: pass.

- [ ] **Step 2: Write the T3 test**

```python
"""The health page's questions against the test stack's real Prometheus and Jaeger
(system health spec §8): every PromQL query is accepted, and an error span sent over OTLP
comes back from Jaeger's API and reduces to a Failure."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Status, StatusCode

from app.modules.monitoring import queries
from app.modules.monitoring.client import JaegerClient, PrometheusClient
from app.modules.monitoring.traces import to_failure

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PROMETHEUS = f"http://localhost:{os.environ.get('PROMETHEUS_PORT', '19090')}"
JAEGER = f"http://localhost:{os.environ.get('JAEGER_UI_PORT', '26686')}"
OTLP = f"http://localhost:{os.environ.get('OTEL_HTTP_PORT', '14318')}"


async def test_every_query_is_valid_promql(stack):
    async with httpx.AsyncClient() as http:
        client = PrometheusClient(PROMETHEUS, http)
        for promql in queries.every_query("1h"):
            await client.query(promql)  # raises SourceUnavailable on a parse error


async def test_an_error_span_comes_back_as_a_failure(stack):
    service = f"health-test-{uuid.uuid4().hex[:8]}"
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(f"{OTLP}/v1/traces")))
    tracer = provider.get_tracer("health-test")
    with tracer.start_as_current_span("POST /appointments"):
        with tracer.start_as_current_span("billing.precheck") as span:
            span.record_exception(RuntimeError("gateway unavailable"))
            span.set_status(Status(StatusCode.ERROR))
    provider.shutdown()

    async with httpx.AsyncClient() as http:
        client = JaegerClient(JAEGER, http)
        end = datetime.now(UTC) + timedelta(minutes=1)
        for _ in range(30):  # the collector batches; give it up to 15 s
            traces = await client.error_traces(service, end - timedelta(hours=1), end, 5)
            if traces:
                break
            await asyncio.sleep(0.5)
    assert traces, "the error span never reached Jaeger"
    failure = to_failure(traces[0])
    assert failure.operation == "POST /appointments"
    assert failure.failed_step == "billing.precheck"
    assert failure.message == "gateway unavailable"
```

Check the test stack's OTLP HTTP port and the markers the docker lane uses:
`grep -n "OTEL_HTTP_PORT" .env.test; grep -n "pytestmark" tests/tiers/t3_integration/test_billing_service.py`.
Use the same markers and the same `stack` fixture as that file.

- [ ] **Step 3: Run it against the test stack**

Bring the stack up with the test-stack skill (`up`), then run:
`python -m pytest tests/tiers/t3_integration/test_monitoring_sources.py -q`
Expected: 2 passed.

If a query is refused, the error names it. Fix it in `queries.py` and re-run Tasks 8 and 13.

- [ ] **Step 4: Commit**

```bash
git add ops/grafana deploy/helm tests/tiers/t3_integration/test_monitoring_sources.py
git commit -m "feat(grafana): panels for the new metrics; T3 proof of the health page's queries"
```

---

### Task 14: Smoke checks

**Files:** Modify: `scripts/k3d_smoke.py`, `scripts/aws_check.sh`

- [ ] **Step 1: k3d smoke.** Find where the smoke logs in as the demo admin
  (`grep -n "demo.admin\|def login\|token" scripts/k3d_smoke.py | head`). After the login step,
  add:

```python
    overview = httpx.get(f"{base}/api/monitoring/overview?hours=1",
                         headers={"Authorization": f"Bearer {token}"}, timeout=20).json()
    down = [c["name"] for c in overview["components"] if c["status"] != "ok"]
    if overview["sources"]["prometheus"] != "ok" or down:
        raise SystemExit(f"health page: prometheus={overview['sources']['prometheus']}, down={down}")
```

Use the file's existing variable names for the base URL and the admin token.

- [ ] **Step 2: aws_check.** After the demo-login `want` lines, add the same check with `curl`
  and `python3 -c`. Use the file's existing token variable:
  `grep -n "TOKEN\|access_token" scripts/aws_check.sh | head -3`.

```bash
HEALTH="$(curl -s -H "Authorization: Bearer $TOKEN" "$B/api/monitoring/overview?hours=1")"
want "health page sees every component" "ok" "$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
o = json.load(sys.stdin)
down = [c["name"] for c in o["components"] if c["status"] != "ok"]
print("ok" if o["sources"]["prometheus"] == "ok" and not down else f"prometheus={o[\"sources\"][\"prometheus\"]} down={down}")')"
```

- [ ] **Step 3: Syntax checks**

Run: `bash -n scripts/aws_check.sh && python -m pytest tests/meta/test_aws_scripts.py -q && python -m ruff check scripts/k3d_smoke.py`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/k3d_smoke.py scripts/aws_check.sh
git commit -m "test(smoke): k3d and AWS checks require every component healthy on the health page"
```

---

### Task 15: Documentation

**Files:** Modify: `docs/PRD.md`, `docs/ARCHITECTURE.md`, `README.md`, `docs/index.html`, `CLAUDE.md`, `docs/data/*.json` (generated)

- [ ] **Step 1: PRD.** Add **D-55** after D-54, in the same table format. Copy the shape of
  the D-54 row:
  - **Decision:** an admin System health page and a System health snapshot report, built
    from Prometheus and Jaeger by a Part A `monitoring` module.
  - **Alternatives rejected:** a separate ops service (a sixth image and chart on an 8 GB
    box); the browser querying Prometheus/Jaeger directly (PromQL in the client, a second
    login, no source for the assistant); embedding Grafana (still an engineering view behind
    a second login).
  - **Traces to:** Week 5 "AI monitoring metrics", Part A §6, Part B Observability.
  - **Recorded:** error messages from traces go to the LLM provider in the AI report.

  Add requirement-trace lines wherever the PRD maps features to requirements:
  `grep -n "AI monitoring\|PART-B-OBS\|Observability" docs/PRD.md`.

- [ ] **Step 2: ARCHITECTURE.** Add a "Monitoring module" subsection beside the analytics
  module's, covering:
  - the sources;
  - the two endpoints;
  - the thresholds table (copy from spec §4.3);
  - that it imports no other module;
  - the heartbeat rule, the Celery and knowledge metrics, and that the cache is in-process.

- [ ] **Step 3: README.** Add the two endpoints to the endpoint/role table (admin), and
  "System health" to the "where every UI is" list (`/health` in the demo client).

- [ ] **Step 4: Site.** In `docs/index.html`:
  - add a feature entry for System health;
  - add the snapshot report to the reports list;
  - add a row to the tools/alternatives table ("Admin health view": in-app page over
    Prometheus+Jaeger, chosen; versus Grafana embed, an ops service, or the browser querying
    directly).

  Regenerate the data with `python scripts/gen_docs_data.py`.

- [ ] **Step 5: CLAUDE.md.** In the Week 5 paragraph, after the AI usage pages sentence, add:
  "**System health** (2026-11-17, PRD D-55): `app/modules/monitoring/` reads Prometheus and
  Jaeger (`SMARTHEALTH_PROMETHEUS_URL`, `SMARTHEALTH_JAEGER_QUERY_URL`) for admin-only
  `GET /monitoring/overview` and `/monitoring/failures`. It feeds the demo client's System
  health page and the assistant's `system_health` report. Every process exports a
  `smarthealth.heartbeat` gauge; Celery task outcomes and knowledge searches are now metrics."

- [ ] **Step 6: Check**

Run: `python scripts/gen_docs_data.py --check && python -m pytest tests/meta -q`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add docs README.md CLAUDE.md
git commit -m "docs: System health page and report (D-55)"
```

---

### Task 16: Full verification, then merge

- [ ] **Step 1: Every lane**

```bash
python -m ruff check app tests services
python -m pytest -m "not docker" -q
python -m pytest -m docker -q          # test stack up (test-stack skill)
cd frontend && npm test && npm run build
```

Expected: all green. Record the counts. They replace the numbers in CLAUDE.md's test harness
row if that row is updated.

- [ ] **Step 2: See it working.** Bring the whole system up:
  `docker compose --profile app --profile partb -f docker-compose.infra.yml up -d --build --wait`.
  Then:
  - log in as an admin at http://localhost:5173 and open **System health**;
  - confirm every component is green within a minute;
  - make a booking fail (`curl -X POST localhost:8090/_chaos -H 'content-type: application/json' -d '{"failure_rate": 1}'`, then book);
  - confirm it shows under Recent failures with the billing step;
  - click **Write a health report** and read it on Reports;
  - reset chaos with `-d '{}'`.

- [ ] **Step 3: Merge** (the project's rule: branch first, fast-forward main, push)

```bash
git checkout main && git merge --ff-only feat/system-health && git push origin main
```

- [ ] **Step 4: AWS (only if the user asks).** `scripts/release.sh`, then `bash scripts/aws_start.sh` and `bash scripts/aws_check.sh`.
