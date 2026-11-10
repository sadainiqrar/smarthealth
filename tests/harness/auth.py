"""Tokens the application will accept, for tests.

Since Week 3 a token is honoured only while its session exists, so minting one means
opening a session too. The contract tier has no Redis: the shared application's session
store and rate limiter are replaced by the in-memory implementations below (installed by
tests/conftest.py), and `issue_token` opens its session there. Those implementations
pass the same behaviour tests as the Redis ones (tests/shared/store_behaviour.py).

The integration tier opens real Redis sessions instead (its `token_for` fixture).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.ratelimit import InMemoryRateLimiter
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.modules.identity.sessions import InMemorySessionStore, SessionStore
from app.settings import Settings

TEST_SESSIONS = InMemorySessionStore()
TEST_LIMITER = InMemoryRateLimiter()


async def open_session_token(
    store: SessionStore, *, subject: str, role: UserRole, settings: Settings,
    now: datetime | None = None,
) -> str:
    issued = await store.issue(user_id=subject, role=role.value, lifetime=timedelta(hours=1))
    return create_access_token(
        subject=subject, role=role, session_id=issued.session_id, settings=settings,
        now=now or datetime.now(UTC),
    )


def issue_token(
    *, subject: str, role: UserRole, settings: Settings | None = None,
    now: datetime | None = None, store: InMemorySessionStore = TEST_SESSIONS,
) -> str:
    """A token with a live session in the in-memory store. Synchronous, because most
    call sites build headers at module level or in plain helpers."""
    issued = store.issue_now(user_id=subject, role=role.value)
    return create_access_token(
        subject=subject, role=role, session_id=issued.session_id,
        settings=settings or Settings(), now=now or datetime.now(UTC),
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
