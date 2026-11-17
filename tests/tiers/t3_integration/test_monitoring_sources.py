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
