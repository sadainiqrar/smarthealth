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

PROMETHEUS = os.environ.get(
    "SMARTHEALTH_TEST_PROMETHEUS", f"http://localhost:{os.environ.get('PROMETHEUS_PORT', '19090')}"
)
JAEGER = os.environ.get(
    "SMARTHEALTH_TEST_JAEGER", f"http://localhost:{os.environ.get('JAEGER_UI_PORT', '26686')}"
)
OTLP = os.environ.get(
    "SMARTHEALTH_TEST_OTLP_HTTP", f"http://localhost:{os.environ.get('OTEL_HTTP_PORT', '14318')}"
)


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
        for _ in range(120):  # the collector batches; give it up to 60 s
            traces = await client.error_traces(service, end - timedelta(hours=1), end, 5)
            if traces:
                break
            await asyncio.sleep(0.5)
    assert traces, "the error span never reached Jaeger"
    failure = to_failure(traces[0])
    assert failure is not None, traces[0]
    assert failure.operation == "POST /appointments"
    assert failure.failed_step == "billing.precheck"
    assert failure.message == "gateway unavailable"


async def test_a_counter_born_with_runs_already_on_it_is_counted_from_zero(stack):
    """The bug the live run found: a counter whose series first appears at 4 (four runs
    inside the first export interval) read as 0 runs, because `increase()` never counts a
    series' first sample. `queries.runs` must count it as 4; the bare `increase` shows the
    gap it closes. Against the real collector and Prometheus, not a string check."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    service = f"health-test-{uuid.uuid4().hex[:8]}"
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{OTLP}/v1/metrics"), export_interval_millis=3_600_000)
    provider = MeterProvider(resource=Resource.create({"service.name": service}),
                             metric_readers=[reader])
    counter = provider.get_meter("health-test").create_counter("health_test_runs", unit="1")
    counter.add(4, {"outcome": "done"})
    provider.force_flush()  # one export: the series is born at 4

    selector = f'health_test_runs_total{{job="{service}"}}'
    async with httpx.AsyncClient() as http:
        client = PrometheusClient(PROMETHEUS, http)
        for _ in range(60):  # Prometheus scrapes the collector every 10 s
            if await client.query(selector):
                break
            await asyncio.sleep(1)
        assert await client.query(selector), "the counter never reached Prometheus"
        await asyncio.sleep(12)  # a second scrape, so increase() has two samples to compare
        old = await client.query(f"sum(increase({selector}[1h]))")
        new = await client.query(queries.runs(selector, "1h"))
    provider.shutdown()
    assert round(old[0].value) == 0  # the gap: the first value is not an increase
    assert round(new[0].value) == 4
