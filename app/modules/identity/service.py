"""Authentication: login, refresh, logout, deactivation.

No FastAPI import: the CLI and, later, consumers call these functions directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidCredentials, NotFound
from app.core.ratelimit import RateLimiter
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.modules.identity.sessions import RotationOutcome, SessionStore
from app.settings import Settings

logger = logging.getLogger(__name__)

#: Verified against when no user matches, so that a failed login costs the same
#: argon2 work whether or not the address exists. Without this the response time
#: alone reveals which addresses are registered, defeating the identical message.
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")

_LOGIN_FAILED = "email or password is incorrect"


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    #: Seconds the access token lives.
    expires_in: int


def _email_key(email: str) -> str:
    # Hashed: the throttle's keys are visible to anyone reading Redis, and a list of the
    # addresses people tried is exactly the enumeration the login refuses to give away.
    return "login:email:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _ip_key(client_ip: str) -> str:
    return f"login:ip:{client_ip}"


def _tokens(
    *, user_id: str, role: UserRole, session_id: str, refresh_token: str,
    settings: Settings, now: datetime,
) -> Tokens:
    access = create_access_token(
        subject=user_id, role=role, session_id=session_id, settings=settings, now=now
    )
    return Tokens(access, refresh_token, settings.access_token_minutes * 60)


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    client_ip: str,
    sessions: SessionStore,
    limiter: RateLimiter,
    settings: Settings,
    now: datetime,
) -> Tokens:
    """Open a session and return its tokens.

    Every failure path raises the same error with the same message, and costs the
    same time. Distinguishing "no such account" from "wrong password" tells an
    attacker which addresses are registered, and an inactive account should not be
    enumerable either.

    The identical message alone is not enough. `or` short-circuits, so an unknown
    address would never reach `verify_password` while a known one pays argon2's
    deliberate ~58ms — a gap trivially readable over a network. So `verify_password`
    runs exactly once on every path, against `_DUMMY_PASSWORD_HASH` when there is no
    user to check, and its result is only consulted afterwards.

    **Throttling keeps that property.** An address with `login_max_failures` failures in
    the window, or a client IP with `login_ip_max_failures`, is refused -- even with the
    right password -- with the same 401 after the same argon2 work. A distinct 429 would
    tell an attacker which addresses exist (only real ones get locked); a fast refusal
    would tell them by timing. The cost is that a locked-out user is not told why; the
    lock lifts by itself when the window ends.

    **The hash runs in a thread, and that is not an optimisation.** argon2 is CPU-bound and
    measured at ~58ms; called directly from this coroutine it blocks the event loop for
    that whole time, which stalls *every other request on the worker*, not just this
    one. `asyncio.to_thread` moves the CPU work off the loop while preserving the property
    that every path costs the same — the thread does identical work either way.
    """
    email_key, ip_key = _email_key(email), _ip_key(client_ip)
    window = timedelta(minutes=settings.login_window_minutes)
    throttled = await limiter.reached(
        email_key, limit=settings.login_max_failures
    ) or await limiter.reached(ip_key, limit=settings.login_ip_max_failures)

    user = await session.scalar(select(User).where(User.email == email))
    stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_matches = await asyncio.to_thread(verify_password, password, stored_hash)
    if throttled or user is None or not user.is_active or not password_matches:
        await limiter.hit(email_key, limit=settings.login_max_failures, window=window)
        await limiter.hit(ip_key, limit=settings.login_ip_max_failures, window=window)
        if throttled:
            logger.warning("login refused by throttle", extra={"client_ip": client_ip})
        raise InvalidCredentials(_LOGIN_FAILED)

    await limiter.reset(email_key)
    issued = await sessions.issue(
        user_id=str(user.id), role=user.role.value,
        lifetime=timedelta(hours=settings.session_hours),
    )
    return _tokens(
        user_id=str(user.id), role=user.role, session_id=issued.session_id,
        refresh_token=issued.refresh_token, settings=settings, now=now,
    )


async def refresh(
    refresh_token: str, *, sessions: SessionStore, settings: Settings, now: datetime
) -> Tokens:
    """Exchange a refresh token for a new pair. The old refresh token is spent.

    A replayed refresh token ends the session (see `sessions`) and is refused with the
    same 401 as an unknown one: the caller learns nothing about which it was.
    """
    rotation = await sessions.rotate(refresh_token)
    if rotation.outcome is RotationOutcome.REUSED:
        logger.warning(
            "refresh token reused; session revoked",
            extra={"session_id": rotation.session_id, "user_id": rotation.user_id},
        )
    if rotation.outcome is not RotationOutcome.ROTATED:
        raise InvalidCredentials("refresh token is not valid")
    assert rotation.user_id and rotation.role and rotation.session_id and rotation.refresh_token
    return _tokens(
        user_id=rotation.user_id, role=UserRole(rotation.role),
        session_id=rotation.session_id, refresh_token=rotation.refresh_token,
        settings=settings, now=now,
    )


async def logout(session_id: str, *, sessions: SessionStore) -> None:
    await sessions.revoke(session_id)


async def logout_everywhere(user_id: str, *, sessions: SessionStore) -> int:
    return await sessions.revoke_user(user_id)


async def deactivate_user(
    session: AsyncSession, *, email: str, sessions: SessionStore
) -> tuple[User, int]:
    """Stop a login from logging in, and end every session it has.

    Commits. The order matters: the row first, so no new session can be opened once the
    sessions are revoked; then the sessions. A crash between the two leaves sessions that
    still work for at most `session_hours` -- repeating the command finishes the job.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        raise NotFound(f"no login with email {email}")
    user.is_active = False
    await session.commit()
    revoked = await sessions.revoke_user(str(user.id))
    return user, revoked
