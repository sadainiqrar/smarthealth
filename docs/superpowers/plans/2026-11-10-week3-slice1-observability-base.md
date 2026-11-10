# Week 3 slice 1 — Observability base: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Part A process (API, Temporal worker) emits traces and metrics through one OpenTelemetry Collector into Jaeger and Prometheus, Grafana shows them from provisioned files, and every JSON log line carries the trace it belongs to.

**Architecture:** `app/core/telemetry.py` (framework-free) builds and installs one tracer provider and one meter provider per process, "first install wins", so tests can install in-memory exporters before anything else (harness requirement 5). `app/main.py` instruments FastAPI at app creation (instrumented against the global proxy providers, which resolve once a real provider is installed); the lifespan and the worker install the real providers. Temporal tracing comes from the SDK's `TracingInterceptor` on the client, which the worker inherits; Temporal's own SDK metrics go OTLP/gRPC from its Rust core to the Collector. Spec: `docs/superpowers/specs/2026-11-10-weeks3-5-architecture-design.md` §7, D-28.

**Tech Stack:** opentelemetry-sdk 1.27+, OTLP/HTTP exporters, opentelemetry-instrumentation-fastapi / -sqlalchemy, `temporalio.contrib.opentelemetry`; otel/opentelemetry-collector-contrib 0.111.0, jaegertracing/all-in-one 1.62.0, prom/prometheus v2.54.1, grafana/grafana 11.2.2.

**Out of scope (later slices):** Redis/Mongo/Celery/Kafka instrumentation (each lands with the slice that uses it), alert rules, RabbitMQ/Kafka exporters, business metrics.

---

## File map

| File | Responsibility |
| --- | --- |
| `pyproject.toml` | new dependencies |
| `app/settings.py` | `otel_endpoint_http`, `otel_endpoint_grpc` |
| `app/core/telemetry.py` (new) | build / install / flush providers; instrument an engine; Temporal runtime |
| `app/core/logging.py` | `trace_id`, `span_id` on every record emitted inside a span |
| `app/core/temporal.py` | `TracingInterceptor` on every client; optional runtime |
| `app/main.py` | FastAPI instrumentation; install telemetry in the lifespan |
| `app/worker.py` | install telemetry; Temporal metrics runtime |
| `tests/conftest.py` | session-wide in-memory exporter + metric reader, installed first |
| `docker-compose.infra.yml`, `.env.test` | four new services, env for `app`/`worker` |
| `ops/otel-collector/config.yaml`, `ops/prometheus/prometheus.yml`, `ops/grafana/**` (new) | pipeline and provisioning |
| tests | T0 `test_telemetry.py`, `test_log_correlation.py`; T1 `test_http_telemetry.py`; T2 `test_temporal_tracing.py`; T3 `test_telemetry_pipeline.py` |
| `tests/cases/obs-00{1,2,3}-*.yaml` | catalog cases (the Stop gate) |

---

### Task 1: Dependencies and settings

**Files:** Modify `pyproject.toml`, `app/settings.py`; Test `tests/tiers/t0_unit/test_settings.py`

- [ ] Add to `dependencies` in `pyproject.toml`:

```toml
    "opentelemetry-api>=1.27,<2",
    "opentelemetry-sdk>=1.27,<2",
    "opentelemetry-exporter-otlp-proto-http>=1.27,<2",
    "opentelemetry-instrumentation-fastapi>=0.48b0",
    "opentelemetry-instrumentation-sqlalchemy>=0.48b0",
```

- [ ] `pip install -e ".[dev]"`; confirm `python -c "import temporalio.contrib.opentelemetry"` works.
- [ ] Failing test in `test_settings.py`:

```python
def test_telemetry_export_is_off_unless_an_endpoint_is_configured():
    settings = Settings(_env_file=None)
    assert settings.otel_endpoint_http == ""
    assert settings.otel_endpoint_grpc == ""
```

- [ ] Add to `Settings` (after `log_level`):

```python
    # --- OpenTelemetry (Week 3) ---
    # Empty means "build providers, export nowhere": tests and a bare laptop run with no
    # collector and no connection errors. Compose sets both to the collector.
    otel_endpoint_http: str = ""   # OTLP/HTTP base, e.g. http://otel-collector:4318
    otel_endpoint_grpc: str = ""   # OTLP/gRPC, used by Temporal's core for SDK metrics
```

- [ ] Run `python -m pytest tests/tiers/t0_unit/test_settings.py -q` → pass. Commit `feat(core): settings and dependencies for OpenTelemetry`.

### Task 2: `app/core/telemetry.py`

**Files:** Create `app/core/telemetry.py`; Test `tests/tiers/t0_unit/test_telemetry.py`

- [ ] Tests (T0, no global state touched — they use `build_telemetry` only):

```python
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.telemetry import build_telemetry

pytestmark = pytest.mark.unit


def test_spans_carry_the_service_name_and_environment():
    exporter = InMemorySpanExporter()
    telemetry = build_telemetry(
        "part-a-api", "test", span_processors=[SimpleSpanProcessor(exporter)]
    )
    with telemetry.tracer_provider.get_tracer("t").start_as_current_span("work"):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.resource.attributes["service.name"] == "part-a-api"
    assert span.resource.attributes["deployment.environment"] == "test"
    assert span.resource.attributes["service.namespace"] == "smarthealth"


def test_metrics_reach_an_injected_reader():
    reader = InMemoryMetricReader()
    telemetry = build_telemetry("part-a-api", "test", metric_readers=[reader])
    telemetry.meter_provider.get_meter("t").create_counter("probe").add(2)
    names = [
        m.name
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    ]
    assert "probe" in names


def test_without_an_endpoint_nothing_exports_over_the_network():
    telemetry = build_telemetry("part-a-api", "test")
    assert telemetry.exports_to is None


def test_with_an_endpoint_both_signals_export_to_it():
    telemetry = build_telemetry("part-a-api", "test", otlp_http_endpoint="http://c:4318")
    assert telemetry.exports_to == "http://c:4318"
    telemetry.shutdown()
```

- [ ] Implementation:

```python
"""OpenTelemetry providers for one process (spec D-28; harness requirement 5).

`build_telemetry` is pure: it returns providers and touches no global. `install`
makes one of them the process-global provider, and the FIRST install wins -- the test
suite installs in-memory exporters before the application is imported, and the
application's own install then reuses them instead of replacing them. OpenTelemetry
refuses to replace a global provider anyway; making that explicit here is what keeps
the providers swappable rather than accidentally fixed.

FastAPI instrumentation lives in app/main.py, not here: app/core is framework-free.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from app.settings import Settings

logger = logging.getLogger(__name__)

#: How often metrics leave the process. Prometheus scrapes the collector every 10 s.
METRIC_EXPORT_INTERVAL_MS = 10_000


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    exports_to: str | None

    def flush(self) -> None:
        """Push buffered spans and metrics out. Safe to call repeatedly, unlike
        shutdown -- the lifespan runs once per test in T1, on the one global provider."""
        self.tracer_provider.force_flush()
        self.meter_provider.force_flush()

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def build_telemetry(
    service_name: str,
    environment: str,
    *,
    otlp_http_endpoint: str = "",
    span_processors: Sequence[SpanProcessor] = (),
    metric_readers: Sequence[MetricReader] = (),
) -> Telemetry:
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "smarthealth",
            "deployment.environment": environment,
        }
    )
    readers = list(metric_readers)
    if otlp_http_endpoint:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{otlp_http_endpoint}/v1/metrics"),
                export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
            )
        )
    tracer_provider = TracerProvider(resource=resource)
    for processor in span_processors:
        tracer_provider.add_span_processor(processor)
    if otlp_http_endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_http_endpoint}/v1/traces"))
        )
    return Telemetry(
        tracer_provider=tracer_provider,
        meter_provider=MeterProvider(resource=resource, metric_readers=readers),
        exports_to=otlp_http_endpoint or None,
    )


_installed: Telemetry | None = None


def install(telemetry: Telemetry) -> Telemetry:
    """Make `telemetry` the process-global provider, unless one already is."""
    global _installed
    if _installed is None:
        trace.set_tracer_provider(telemetry.tracer_provider)
        metrics.set_meter_provider(telemetry.meter_provider)
        _installed = telemetry
    return _installed


def setup_telemetry(settings: Settings, service_name: str) -> Telemetry:
    """Install this process's providers from settings, or return the ones installed."""
    if _installed is not None:
        return _installed
    return install(
        build_telemetry(
            service_name, settings.environment, otlp_http_endpoint=settings.otel_endpoint_http
        )
    )


def instrument_engine(engine: AsyncEngine) -> None:
    """Trace every SQL statement on `engine`. One engine per process: the instrumentor
    is a singleton, so a second engine (T1 runs the lifespan per test) is left alone."""
    instrumentor = SQLAlchemyInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(engine=engine.sync_engine)
```

- [ ] Run `python -m pytest tests/tiers/t0_unit/test_telemetry.py -q` → pass. Commit `feat(core): build and install OpenTelemetry providers`.

### Task 3: Trace ids in JSON logs

**Files:** Modify `app/core/logging.py`; Test `tests/tiers/t0_unit/test_log_correlation.py`

- [ ] Tests:

```python
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
```

- [ ] In `JsonFormatter.format`, after the `message` key, add (and `from opentelemetry import trace` at the top; update the module docstring's "join this record in Week 3" sentence to say they now do):

```python
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["trace_id"] = format(context.trace_id, "032x")
            payload["span_id"] = format(context.span_id, "016x")
```

- [ ] Run the new test and `tests/tiers/t0_unit -q -k log` → pass. Commit `feat(core): correlate JSON log lines with their trace`.

### Task 4: Session-wide in-memory telemetry for the suite

**Files:** Modify `tests/conftest.py`

- [ ] At import time of `tests/conftest.py` (before any `app.main` import is collected), install in-memory telemetry and expose fixtures:

```python
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import telemetry as _telemetry

#: Installed once, before the application is imported, so the application's own
#: `setup_telemetry` finds it and reuses it (first install wins -- harness requirement 5).
SPAN_EXPORTER = InMemorySpanExporter()
METRIC_READER = InMemoryMetricReader()
_telemetry.install(
    _telemetry.build_telemetry(
        "smarthealth-tests", "test",
        span_processors=[SimpleSpanProcessor(SPAN_EXPORTER)],
        metric_readers=[METRIC_READER],
    )
)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Finished spans recorded during this test only."""
    SPAN_EXPORTER.clear()
    return SPAN_EXPORTER


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    return METRIC_READER
```

- [ ] `python -m pytest -m "not docker" -q -x` → still green. Commit `test: install in-memory telemetry for the whole suite`.

### Task 5: Instrument the API

**Files:** Modify `app/main.py`; Test `tests/tiers/t1_contract/test_http_telemetry.py`

- [ ] Tests:

```python
import pytest
from opentelemetry.trace import SpanKind

pytestmark = pytest.mark.contract


async def test_a_request_produces_a_server_span_named_by_its_route(api_client, spans):
    await api_client.post("/auth/login", json={})
    server = [s for s in spans.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert server, "no SERVER span recorded"
    assert server[-1].attributes["http.route"] == "/auth/login"
    assert server[-1].attributes["http.status_code"] == 422


async def test_health_probes_are_not_traced(api_client, spans):
    await api_client.get("/health")
    assert not [s for s in spans.get_finished_spans() if s.kind is SpanKind.SERVER]


async def test_request_duration_is_recorded_as_a_metric(api_client, metric_reader):
    await api_client.post("/auth/login", json={})
    names = {
        m.name
        for rm in metric_reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert names & {"http.server.duration", "http.server.request.duration"}
```

- [ ] In `app/main.py`: import `FastAPIInstrumentor` and `setup_telemetry`, `instrument_engine`. In `create_app()` after `register_error_handlers`:

```python
    # At creation, not in the lifespan: Starlette refuses new middleware once the app
    # has started. The instrumentation binds to the global proxy providers, which
    # resolve to the real ones once the lifespan installs them.
    FastAPIInstrumentor.instrument_app(application, excluded_urls="/health$,/ready$")
```

In `lifespan`, after `configure_logging`: `app.state.telemetry = setup_telemetry(settings, "part-a-api")`; after the engine is created: `instrument_engine(app.state.engine)`; in `finally` first line: `app.state.telemetry.flush()`.

- [ ] If `http.status_code` is absent (new semantic conventions), assert on `http.response.status_code` instead — check `server[-1].attributes` and use whichever the installed version emits.
- [ ] Run `python -m pytest tests/tiers/t1_contract -q` → pass. Commit `feat(api): trace and measure every request`.

### Task 6: Trace through Temporal; worker telemetry

**Files:** Modify `app/core/temporal.py`, `app/worker.py`; Test `tests/tiers/t2_workflow/test_temporal_tracing.py`

- [ ] Test:

```python
import uuid

import pytest
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from app.core.temporal import build_worker
from app.core.temporal_canary import CanaryWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


async def test_starting_and_running_a_workflow_share_one_trace(temporal_env, spans):
    config = temporal_env.client.config()
    config["interceptors"] = [TracingInterceptor()]
    client = Client(**config)
    queue = f"tracing-{uuid.uuid4()}"
    async with build_worker(client, queue):
        await client.execute_workflow(
            CanaryWorkflow.run, id=f"canary-{uuid.uuid4()}", task_queue=queue
        )
    by_name = {s.name: s for s in spans.get_finished_spans()}
    start, run = by_name["StartWorkflow:Canary"], by_name["RunWorkflow:Canary"]
    assert start.context.trace_id == run.context.trace_id
```

- [ ] In `connect_client`: add `runtime: Runtime | None = None` param and pass `interceptors=[TracingInterceptor()]` and `runtime=runtime` (only when not None — `Client.connect(..., **({"runtime": runtime} if runtime else {}))`). Add a helper:

```python
def telemetry_runtime(settings: Settings) -> Runtime | None:
    """A Temporal runtime exporting the SDK's own metrics (workflow/activity outcomes,
    schedule-to-start latency, task-queue polls) over OTLP/gRPC, or None for none."""
    if not settings.otel_endpoint_grpc:
        return None
    return Runtime(
        telemetry=TelemetryConfig(
            metrics=OpenTelemetryConfig(
                url=settings.otel_endpoint_grpc, metric_periodicity=timedelta(seconds=10)
            )
        )
    )
```

(`from temporalio.runtime import OpenTelemetryConfig, Runtime, TelemetryConfig`; check parameter names against the installed temporalio.)

- [ ] If the T2 test fails with a sandbox import error on `opentelemetry`, pass `workflow_runner=SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules("opentelemetry"))` in `build_worker`.
- [ ] In `app/worker.py` `_run`: `telemetry = setup_telemetry(settings, "part-a-worker")` first; `client = await connect_client(settings, lazy=False, runtime=telemetry_runtime(settings))`; `instrument_engine(engine)` after it is created; `telemetry.flush()` in the `finally`.
- [ ] Run `python -m pytest -m workflow -q` → pass. Commit `feat(worker): trace workflows from start to activity, export SDK metrics`.

### Task 7: Compose — collector, Jaeger, Prometheus, Grafana

**Files:** Modify `docker-compose.infra.yml`, `.env.test`, `tests/tiers/t3_integration/test_stack_boots.py`; Create `ops/otel-collector/config.yaml`, `ops/prometheus/prometheus.yml`

- [ ] `ops/otel-collector/config.yaml`:

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }
processors:
  memory_limiter: { check_interval: 1s, limit_mib: 256 }
  batch: {}
exporters:
  otlp/jaeger:
    endpoint: jaeger:4317
    tls: { insecure: true }
  prometheus:
    endpoint: 0.0.0.0:8889
    resource_to_telemetry_conversion: { enabled: true }
extensions:
  health_check: { endpoint: 0.0.0.0:13133 }
service:
  extensions: [health_check]
  pipelines:
    traces:  { receivers: [otlp], processors: [memory_limiter, batch], exporters: [otlp/jaeger] }
    metrics: { receivers: [otlp], processors: [memory_limiter, batch], exporters: [prometheus] }
```

- [ ] `ops/prometheus/prometheus.yml`: global `scrape_interval: 10s`; jobs `otel-collector` (`otel-collector:8889`, `honor_labels: true`) and `prometheus` (`localhost:9090`).
- [ ] Services in `docker-compose.infra.yml` (no profile — the test stack proves the pipeline): `otel-collector` (contrib 0.111.0, config mounted at `/etc/otelcol-contrib/config.yaml`, ports `${OTEL_GRPC_PORT:-4317}:4317`, `${OTEL_HTTP_PORT:-4318}:4318`, depends on jaeger; distroless image, so no healthcheck); `jaeger` (all-in-one 1.62.0, `COLLECTOR_OTLP_ENABLED=true`, `${JAEGER_UI_PORT:-16686}:16686`); `prometheus` (v2.54.1, config mounted, `--web.enable-lifecycle`, `${PROMETHEUS_PORT:-9090}:9090`, healthcheck `wget -qO- http://localhost:9090/-/ready`); `grafana` (11.2.2, anonymous Viewer, `${GRAFANA_PORT:-3000}:3000`, provisioning mounts from Task 8, healthcheck `wget -qO- http://localhost:3000/api/health`). Add `SMARTHEALTH_OTEL_ENDPOINT_HTTP: http://otel-collector:4318` and `SMARTHEALTH_OTEL_ENDPOINT_GRPC: http://otel-collector:4317` to `app` and `worker`.
- [ ] `.env.test`: `OTEL_GRPC_PORT=14317`, `OTEL_HTTP_PORT=14318`, `JAEGER_UI_PORT=26686`, `PROMETHEUS_PORT=19090`, `GRAFANA_PORT=13000`.
- [ ] Extend `EXPECTED_SERVICES` with `"otel-collector", "jaeger", "prometheus", "grafana"`.
- [ ] `docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml up -d --wait` → every service healthy/running. Commit `feat(compose): OpenTelemetry Collector, Jaeger, Prometheus, Grafana`.

### Task 8: Grafana provisioning and the Service overview dashboard

**Files:** Create `ops/grafana/provisioning/datasources/datasources.yaml`, `ops/grafana/provisioning/dashboards/dashboards.yaml`, `ops/grafana/dashboards/service-overview.json`

- [ ] Datasources: Prometheus (`uid: prometheus`, `http://prometheus:9090`, default) and Jaeger (`uid: jaeger`, `http://jaeger:16686`). Dashboards provider: file, path `/var/lib/grafana/dashboards`, folder "SmartHealth".
- [ ] Dashboard panels (timeseries, Prometheus): request rate by route and status; 5xx ratio; p95 latency by route; Temporal workflows completed/failed per type; activity failures; activity schedule-to-start p95. **Before finalising queries**, list real names: `curl -s localhost:19090/api/v1/label/__name__/values | python -m json.tool | grep -E "http_server|temporal_"` after driving a few requests and a booking, and use exactly those names.
- [ ] Restart grafana; `curl -s localhost:13000/api/search?query=Service` lists the dashboard. Commit `feat(ops): provision Grafana datasources and the service overview dashboard`.

### Task 9: Prove the pipeline end to end (docker lane)

**Files:** Create `tests/tiers/t3_integration/test_telemetry_pipeline.py`

- [ ] Test: build a private `Telemetry` (not installed) pointed at `http://localhost:14318` with a unique `service.name`; emit one span and one counter; `flush()`; poll Jaeger `GET localhost:26686/api/traces?service=<name>` and Prometheus `GET localhost:19090/api/v1/query?query=smarthealth_pipeline_probe_total{job="<name>"}` until found, 60 s deadline. Marks `integration`, `docker`; uses the `stack` fixture.
- [ ] Run `python -m pytest tests/tiers/t3_integration/test_telemetry_pipeline.py -q` → pass. Commit `test: a span and a metric cross the collector into Jaeger and Prometheus`.

### Task 10: Cases, docs, full verification

- [ ] Cases (tier/impl form like `sys-006`): `obs-001-every-request-is-traced-and-measured` (contract, PART-A-OBS-2 + OBS-3, impl → Task 5 test 1), `obs-002-a-workflow-run-joins-the-trace-that-started-it` (workflow, PART-A-OBS-2, impl → Task 6), `obs-003-telemetry-reaches-jaeger-and-prometheus` (integration, PART-A-OBS-2 + OBS-3, impl → Task 9). `python -m tests.runner.route_check` → valid; regenerate `tests/cases/CATALOG.md` with `--write-catalog`.
- [ ] Docs: PRD OBS-2/OBS-3 → "Partial — base (slice 1)"; ARCHITECTURE status table rows for the four services and the OTel line; README: URLs for Jaeger/Prometheus/Grafana; CLAUDE.md harness requirement 5 satisfied.
- [ ] `python -m ruff check app tests`; `python -m pytest -m "not docker" -q`; `python -m pytest -m docker -q` → all green. Commit `docs: observability base`.
