import httpx
import pytest

from tests.runner.engine import CaseContext, run_case
from tests.runner.schema import Case

pytestmark = pytest.mark.unit


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver")


async def test_api_step_asserts_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "environment": "test"})

    case = Case.model_validate({
        "id": "sys-020-ok", "title": "OK", "tier": "contract",
        "steps": [{"api": {"path": "/health", "expect": {"status": 200,
                                                         "json_contains": {"status": "ok"}}}}],
    })
    async with make_client(handler) as client:
        await run_case(case, CaseContext(api=client))


async def test_api_step_fails_on_a_status_mismatch():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    case = Case.model_validate({
        "id": "sys-021-bad-status", "title": "Bad status", "tier": "contract",
        "steps": [{"api": {"path": "/health", "expect": {"status": 200}}}],
    })
    async with make_client(handler) as client:
        with pytest.raises(AssertionError) as exc:
            await run_case(case, CaseContext(api=client))
    assert "step 1" in str(exc.value)
    assert "expected status 200" in str(exc.value)


async def test_json_contains_is_a_recursive_subset_check():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outer": {"inner": "value", "extra": 1}})

    case = Case.model_validate({
        "id": "sys-022-nested", "title": "Nested", "tier": "contract",
        "steps": [{"api": {"path": "/x",
                           "expect": {"json_contains": {"outer": {"inner": "value"}}}}}],
    })
    async with make_client(handler) as client:
        await run_case(case, CaseContext(api=client))


async def test_json_contains_reports_the_missing_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outer": {}})

    case = Case.model_validate({
        "id": "sys-023-missing", "title": "Missing", "tier": "contract",
        "steps": [{"api": {"path": "/x",
                           "expect": {"json_contains": {"outer": {"inner": "value"}}}}}],
    })
    async with make_client(handler) as client:
        with pytest.raises(AssertionError) as exc:
            await run_case(case, CaseContext(api=client))
    assert "$.outer.inner" in str(exc.value)


async def test_post_body_is_forwarded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content"] = request.content
        return httpx.Response(201, json={})

    case = Case.model_validate({
        "id": "sys-024-post", "title": "Post", "tier": "contract",
        "steps": [{"api": {"method": "POST", "path": "/x", "body": {"a": 1},
                           "expect": {"status": 201}}}],
    })
    async with make_client(handler) as client:
        await run_case(case, CaseContext(api=client))
    assert seen["method"] == "POST"
    assert b'"a":1' in seen["content"].replace(b" ", b"")


async def test_unsupported_step_kind_fails_loudly():
    case = Case.model_validate({
        "id": "sys-025-emit", "title": "Emit", "tier": "integration",
        "steps": [{"emit": {"topic": "appointments.booked"}}],
        "expect": {"events": [{"topic": "appointments.booked", "count": 1}]},
    })
    async with make_client(lambda request: httpx.Response(200)) as client:
        with pytest.raises(NotImplementedError) as exc:
            await run_case(case, CaseContext(api=client))
    assert "emit" in str(exc.value)


async def test_case_level_expect_api_is_asserted_against_the_last_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    case = Case.model_validate({
        "id": "sys-026-final", "title": "Final expectation", "tier": "contract",
        "steps": [{"api": {"path": "/health"}}],
        "expect": {"api": {"status": 200, "json_contains": {"status": "ok"}}},
    })
    async with make_client(handler) as client:
        await run_case(case, CaseContext(api=client))
