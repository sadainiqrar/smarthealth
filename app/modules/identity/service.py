"""Authentication.

No FastAPI import: Week 2's Temporal activities call these functions directly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidCredentials
from app.modules.identity.models import User
from app.modules.identity.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.settings import Settings

#: Verified against when no user matches, so that a failed login costs the same
#: argon2 work whether or not the address exists. Without this the response time
#: alone reveals which addresses are registered, defeating the identical message.
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    settings: Settings,
    now: datetime,
) -> tuple[str, int]:
    """Return an access token and its lifetime in seconds.

    Every failure path raises the same error with the same message, and costs the
    same time. Distinguishing "no such account" from "wrong password" tells an
    attacker which addresses are registered, and an inactive account should not be
    enumerable either.

    The identical message alone is not enough. `or` short-circuits, so an unknown
    address would never reach `verify_password` while a known one pays argon2's
    deliberate ~58ms — a gap trivially readable over a network. So `verify_password`
    runs exactly once on every path, against `_DUMMY_PASSWORD_HASH` when there is no
    user to check, and its result is only consulted afterwards.

    **It runs in a thread, and that is not an optimisation.** argon2 is CPU-bound and
    measured at ~58ms; called directly from this coroutine it blocks the event loop for
    that whole time, which stalls *every other request on the worker*, not just this
    one. The timing defence above makes that reachable without any valid credentials:
    an unknown address pays the same 58ms by design, so a flood of garbage addresses
    freezes the loop just as effectively as a flood of real ones. `asyncio.to_thread`
    moves the CPU work off the loop while preserving the property that every path costs
    the same — the thread does identical work either way.
    """
    user = await session.scalar(select(User).where(User.email == email))
    stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_matches = await asyncio.to_thread(verify_password, password, stored_hash)
    if user is None or not user.is_active or not password_matches:
        raise InvalidCredentials("email or password is incorrect")

    token = create_access_token(subject=str(user.id), role=user.role, settings=settings, now=now)
    return token, settings.jwt_expiry_minutes * 60
