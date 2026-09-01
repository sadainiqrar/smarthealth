"""Fixtures shared across every tier.

Tier-specific fixtures belong in the tier's own conftest; anything here is
available to the whole suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import app
from app.settings import get_settings


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
    """An HTTP client wired straight to the ASGI app — no socket, no server."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
