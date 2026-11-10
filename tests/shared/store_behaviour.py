"""Behaviour every SessionStore and RateLimiter must have, written once.

Imported by a T0 module (the in-memory implementations) and a T3 module (Redis), each
providing `store` / `limiter` fixtures: the contract tier's in-memory stand-ins are only
trustworthy if they pass the same tests as the real thing.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.modules.identity.sessions import RotationOutcome

LIFETIME = timedelta(hours=1)


async def test_an_issued_session_is_active_until_revoked(store):
    issued = await store.issue(user_id="u1", role="admin", lifetime=LIFETIME)
    assert await store.is_active(issued.session_id)
    await store.revoke(issued.session_id)
    assert not await store.is_active(issued.session_id)


async def test_a_refresh_rotates_the_secret_and_keeps_the_session(store):
    issued = await store.issue(user_id="u1", role="patient", lifetime=LIFETIME)
    rotation = await store.rotate(issued.refresh_token)
    assert rotation.outcome is RotationOutcome.ROTATED
    assert rotation.session_id == issued.session_id
    assert (rotation.user_id, rotation.role) == ("u1", "patient")
    assert rotation.refresh_token != issued.refresh_token
    assert await store.is_active(issued.session_id)
    again = await store.rotate(rotation.refresh_token)
    assert again.outcome is RotationOutcome.ROTATED


async def test_replaying_a_used_refresh_token_revokes_the_session(store):
    issued = await store.issue(user_id="u1", role="patient", lifetime=LIFETIME)
    rotated = await store.rotate(issued.refresh_token)
    replay = await store.rotate(issued.refresh_token)
    assert replay.outcome is RotationOutcome.REUSED
    assert not await store.is_active(issued.session_id)
    # The thief's replay also killed the legitimate holder's newer token.
    after = await store.rotate(rotated.refresh_token)
    assert after.outcome is RotationOutcome.UNKNOWN


async def test_an_unknown_secret_is_refused_and_leaves_the_session_alone(store):
    issued = await store.issue(user_id="u1", role="patient", lifetime=LIFETIME)
    forged = f"{issued.session_id}.not-the-secret"
    assert (await store.rotate(forged)).outcome is RotationOutcome.UNKNOWN
    assert (await store.rotate("garbage")).outcome is RotationOutcome.UNKNOWN
    assert await store.is_active(issued.session_id)


async def test_revoking_a_user_ends_all_their_sessions_and_nobody_elses(store):
    first = await store.issue(user_id="u1", role="admin", lifetime=LIFETIME)
    second = await store.issue(user_id="u1", role="admin", lifetime=LIFETIME)
    other = await store.issue(user_id="u2", role="admin", lifetime=LIFETIME)
    assert await store.revoke_user("u1") == 2
    assert not await store.is_active(first.session_id)
    assert not await store.is_active(second.session_id)
    assert await store.is_active(other.session_id)


async def test_a_session_expires_with_its_lifetime(store):
    issued = await store.issue(user_id="u1", role="admin", lifetime=timedelta(seconds=1))
    await asyncio.sleep(1.2)
    assert not await store.is_active(issued.session_id)
    assert (await store.rotate(issued.refresh_token)).outcome is RotationOutcome.UNKNOWN


async def test_hits_within_the_limit_are_allowed_and_the_next_is_not(limiter):
    window = timedelta(minutes=1)
    for _ in range(3):
        assert (await limiter.hit("k", limit=3, window=window)).allowed
    refused = await limiter.hit("k", limit=3, window=window)
    assert not refused.allowed
    assert 0 < refused.retry_after <= 60
    assert await limiter.reached("k", limit=3)


async def test_reached_counts_nothing(limiter):
    window = timedelta(minutes=1)
    await limiter.hit("k", limit=2, window=window)
    for _ in range(5):
        assert not await limiter.reached("k", limit=2)
    assert await limiter.reached("k", limit=1)
    assert (await limiter.hit("k", limit=2, window=window)).allowed


async def test_reset_starts_a_fresh_window(limiter):
    window = timedelta(minutes=1)
    for _ in range(3):
        await limiter.hit("k", limit=1, window=window)
    await limiter.reset("k")
    assert (await limiter.hit("k", limit=1, window=window)).allowed


async def test_the_window_expires(limiter):
    await limiter.hit("k", limit=1, window=timedelta(seconds=1))
    await limiter.hit("k", limit=1, window=timedelta(seconds=1))
    await asyncio.sleep(1.2)
    assert (await limiter.hit("k", limit=1, window=timedelta(seconds=1))).allowed


async def test_keys_are_independent(limiter):
    window = timedelta(minutes=1)
    await limiter.hit("a", limit=1, window=window)
    await limiter.hit("a", limit=1, window=window)
    assert (await limiter.hit("b", limit=1, window=window)).allowed
