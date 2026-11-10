"""Every request is a trace and a measurement; health probes are neither.

`POST /auth/login` with an empty body is used because it is a real route that answers
(422) without touching any infrastructure, so the contract lane needs no containers.
"""

import pytest
from opentelemetry.trace import SpanKind

pytestmark = pytest.mark.contract


def _server_spans(spans):
    return [span for span in spans.get_finished_spans() if span.kind is SpanKind.SERVER]


def _status(span) -> object:
    # The attribute was renamed by the stable HTTP semantic conventions.
    attributes = span.attributes
    return attributes.get("http.response.status_code", attributes.get("http.status_code"))


async def test_a_request_produces_a_server_span_named_by_its_route(api_client, spans):
    await api_client.post("/auth/login", json={})
    server = _server_spans(spans)
    assert server, "no SERVER span recorded"
    assert server[-1].attributes["http.route"] == "/auth/login"
    assert _status(server[-1]) == 422


async def test_health_probes_are_not_traced(api_client, spans):
    await api_client.get("/health")
    assert not _server_spans(spans)


async def test_request_duration_is_recorded_as_a_metric(api_client, metric_reader):
    await api_client.post("/auth/login", json={})
    data = metric_reader.get_metrics_data()
    names = {
        metric.name
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }
    assert names & {"http.server.duration", "http.server.request.duration"}, names
