"""The in-memory session store and rate limiter, against the shared behaviour tests."""

import pytest

from app.core.ratelimit import InMemoryRateLimiter
from app.modules.identity.sessions import InMemorySessionStore
from tests.shared.store_behaviour import *  # noqa: F403 - the shared tests are collected here

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    return InMemorySessionStore()


@pytest.fixture
def limiter():
    return InMemoryRateLimiter()
