"""The Redis session store and rate limiter, against the shared behaviour tests.

The same tests run against the in-memory implementations in T0
(tests/tiers/t0_unit/test_session_store.py): that pairing is what entitles the contract
tier to use the in-memory ones.
"""

from collections.abc import AsyncIterator

import pytest

from app.core.ratelimit import RedisRateLimiter
from app.db.redis import create_redis_client
from app.modules.identity.sessions import RedisSessionStore
from app.settings import Settings
from tests.shared.store_behaviour import *  # noqa: F403 - the shared tests are collected here

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def redis(db_settings: Settings) -> AsyncIterator:
    client = create_redis_client(db_settings)
    try:
        yield client
    finally:
        # Only this run's keys: the prefix isolates parallel runs on one server.
        keys = [key async for key in client.scan_iter(f"{db_settings.redis_prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.fixture
def store(redis, db_settings):
    return RedisSessionStore(redis, db_settings)


@pytest.fixture
def limiter(redis, db_settings):
    return RedisRateLimiter(redis, db_settings)


async def test_two_concurrent_refreshes_with_one_token_cannot_both_win(store):
    """Rotation is one Lua script. A check-then-set from Python would let both rotate,
    leaving two live refresh tokens in one family."""
    import asyncio

    from app.modules.identity.sessions import RotationOutcome

    issued = await store.issue(user_id="u1", role="admin", lifetime=LIFETIME)  # noqa: F405
    outcomes = await asyncio.gather(*(store.rotate(issued.refresh_token) for _ in range(5)))
    rotated = [o for o in outcomes if o.outcome is RotationOutcome.ROTATED]
    assert len(rotated) == 1, [o.outcome for o in outcomes]
