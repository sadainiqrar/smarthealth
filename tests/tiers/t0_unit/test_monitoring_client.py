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
        trace = {"traceID": "abc", "spans": [], "processes": {}}
        return httpx.Response(200, json={"data": [trace]})

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


@pytest.mark.parametrize("body", [
    {"status": "success", "data": {}},
    {"status": "success", "data": {"result": [{"metric": {}, "value": [1.0]}]}},
    {"status": "success", "data": {"result": [{"metric": {}, "value": [1.0, "not a number"]}]}},
    {"status": "success", "data": {"result": [{"value": [1.0, "1"]}]}},
    ["not", "an", "object"],
])
async def test_a_malformed_prometheus_answer_is_unavailable(body):
    async with _client(lambda _r: httpx.Response(200, json=body)) as http:
        with pytest.raises(SourceUnavailable):
            await PrometheusClient("http://prom:9090", http).query("up")


async def test_ping_asks_for_the_service_list_and_any_failure_is_unavailable():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    async with _client(handler) as http:
        await JaegerClient("http://jaeger:16686/jaeger", http).ping()
    assert paths == ["/jaeger/api/services"]

    async with _client(lambda _r: httpx.Response(503)) as http:
        with pytest.raises(SourceUnavailable):
            await JaegerClient("http://jaeger:16686", http).ping()

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(refuse) as http:
        with pytest.raises(SourceUnavailable):
            await JaegerClient("http://jaeger:16686", http).ping()


@pytest.mark.parametrize("data", ["oops", {"a": 1}, 5])
async def test_jaeger_data_that_is_not_a_list_is_unavailable(data):
    async with _client(lambda _r: httpx.Response(200, json={"data": data})) as http:
        with pytest.raises(SourceUnavailable):
            await JaegerClient("http://jaeger:16686", http).error_traces(
                "x", datetime.now(UTC), datetime.now(UTC), 5)
