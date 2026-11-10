"""`build_telemetry` is pure: these tests never touch the process-global providers."""

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.telemetry import build_telemetry

pytestmark = pytest.mark.unit


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    if data is None:
        return set()
    return {
        metric.name
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_spans_carry_the_service_name_namespace_and_environment():
    exporter = InMemorySpanExporter()
    telemetry = build_telemetry(
        "part-a-api", "test", span_processors=[SimpleSpanProcessor(exporter)]
    )
    with telemetry.tracer_provider.get_tracer("t").start_as_current_span("work"):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.resource.attributes["service.name"] == "part-a-api"
    assert span.resource.attributes["service.namespace"] == "smarthealth"
    assert span.resource.attributes["deployment.environment"] == "test"


def test_metrics_reach_an_injected_reader():
    reader = InMemoryMetricReader()
    telemetry = build_telemetry("part-a-api", "test", metric_readers=[reader])
    telemetry.meter_provider.get_meter("t").create_counter("probe").add(2)
    assert "probe" in _metric_names(reader)


def test_without_an_endpoint_nothing_exports_over_the_network():
    telemetry = build_telemetry("part-a-api", "test")
    assert telemetry.exports_to is None
    telemetry.shutdown()


def test_with_an_endpoint_both_signals_export_to_it():
    telemetry = build_telemetry("part-a-api", "test", otlp_http_endpoint="http://c:4318")
    assert telemetry.exports_to == "http://c:4318"
    telemetry.shutdown()
