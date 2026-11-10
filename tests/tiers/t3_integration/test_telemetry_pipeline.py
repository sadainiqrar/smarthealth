"""A span and a metric leave a process, cross the collector, and can be queried.

Every in-process test proves only that spans and metrics are *recorded*. This one proves
the deployment: the collector's config, its two exporters, Jaeger's OTLP intake and
Prometheus's scrape of the collector. A typo in any of those files passes every other
test in the suite and leaves the dashboards empty.

The providers here are built privately and never installed, so the suite's global
in-memory telemetry is untouched; a unique service name keeps parallel runs apart.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from app.core.telemetry import build_telemetry

pytestmark = [pytest.mark.integration, pytest.mark.docker]

COLLECTOR = os.environ.get("SMARTHEALTH_TEST_OTEL_HTTP", "http://localhost:14318")
JAEGER = os.environ.get("SMARTHEALTH_TEST_JAEGER", "http://localhost:26686")
PROMETHEUS = os.environ.get("SMARTHEALTH_TEST_PROMETHEUS", "http://localhost:19090")

#: Batch export, then a 10 s scrape: the metric needs up to ~25 s in the worst case.
DEADLINE_SECONDS = 60.0


def _poll(fetch, deadline: float = DEADLINE_SECONDS):
    """Call `fetch` until it returns something truthy or the deadline passes."""
    end = time.monotonic() + deadline
    while True:
        result = fetch()
        if result or time.monotonic() > end:
            return result
        time.sleep(1.0)


@pytest.fixture
def probe_service(stack) -> str:
    return f"pipeline-probe-{uuid.uuid4().hex[:12]}"


def test_a_span_reaches_jaeger(probe_service):
    telemetry = build_telemetry(probe_service, "test", otlp_http_endpoint=COLLECTOR)
    with telemetry.tracer_provider.get_tracer("probe").start_as_current_span("probe-span"):
        pass
    telemetry.flush()

    def fetch():
        response = httpx.get(f"{JAEGER}/api/traces", params={"service": probe_service})
        return response.status_code == 200 and response.json().get("data")

    traces = _poll(fetch)
    telemetry.shutdown()
    assert traces, f"no trace for {probe_service} reached Jaeger"
    operations = {span["operationName"] for trace in traces for span in trace["spans"]}
    assert "probe-span" in operations


def test_a_metric_reaches_prometheus_labelled_with_its_service(probe_service):
    telemetry = build_telemetry(probe_service, "test", otlp_http_endpoint=COLLECTOR)
    telemetry.meter_provider.get_meter("probe").create_counter(
        "smarthealth_pipeline_probe"
    ).add(1)
    telemetry.flush()

    query = f'smarthealth_pipeline_probe_total{{service_name="{probe_service}"}}'

    def fetch():
        response = httpx.get(f"{PROMETHEUS}/api/v1/query", params={"query": query})
        return response.status_code == 200 and response.json()["data"]["result"]

    result = _poll(fetch)
    telemetry.shutdown()
    assert result, f"`{query}` returned nothing from Prometheus"
    assert float(result[0]["value"][1]) == 1.0
