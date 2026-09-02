"""Fixtures shared across every tier.

Tier-specific fixtures belong in the tier's own conftest; anything here is
available to the whole suite.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import app
from app.settings import get_settings
from tests.harness.isolation import RunIsolation, make_isolation
from tests.harness.stack import TestStack


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """`get_settings` is an lru_cache singleton, so one test's env would otherwise
    leak into every later test in the same process. Reset around every test rather
    than trusting each test to remember."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to the ASGI app — no socket, no server.

    Wrapped in LifespanManager because `httpx.ASGITransport` does not run FastAPI's
    lifespan: without it these tests exercise an app whose startup never ran, and
    `app.state.engine` would not exist.
    """
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


@pytest.fixture(scope="session")
def isolation(request) -> RunIsolation:
    """This run's private slice of the shared test stack."""
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "master")
    return make_isolation(worker_id=worker_id)


@pytest.fixture(scope="session")
def stack() -> TestStack:
    """The shared infrastructure stack.

    Reuses an already-running stack by default so the dev loop pays boot cost once.
    Set SMARTHEALTH_TEST_STACK=fresh to force a boot, or =external to assume someone
    else started it.
    """
    mode = os.environ.get("SMARTHEALTH_TEST_STACK", "reuse")
    instance = TestStack()
    if mode == "external":
        return instance
    if mode == "fresh" or not instance.is_running():
        instance.up()
    return instance
