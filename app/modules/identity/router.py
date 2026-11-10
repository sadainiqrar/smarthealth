"""Authentication routes, and the public verification key."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_rate_limiter, get_session
from app.core.clock import Clock, get_clock
from app.core.ratelimit import RateLimiter
from app.modules.identity.deps import get_session_store, require_role
from app.modules.identity.models import User, UserRole
from app.modules.identity.schemas import (
    JwksResponse,
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    TokenResponse,
    UserDirectoryEntry,
)
from app.modules.identity.security import TokenClaims, public_jwk
from app.modules.identity.service import authenticate, logout, logout_everywhere, refresh
from app.modules.identity.sessions import SessionStore
from app.modules.patients.public import names_for_users as patient_names
from app.modules.providers.public import names_for_users as provider_names

router = APIRouter(prefix="/auth", tags=["auth"])
jwks_router = APIRouter(tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])

ANY_ROLE = tuple(UserRole)


def _response(tokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    sessions: SessionStore = Depends(get_session_store),
    limiter: RateLimiter = Depends(get_rate_limiter),
    clock: Clock = Depends(get_clock),
) -> TokenResponse:
    # The socket's peer, never X-Forwarded-For: that header is the client's to write, and
    # trusting it would let a guesser pick a fresh "IP" per attempt. Behind a proxy this
    # is the proxy, which makes the per-IP limit a global one -- conservative, and the
    # per-address limit still applies to each account.
    client_ip = request.client.host if request.client else "unknown"
    tokens = await authenticate(
        session,
        email=body.email,
        password=body.password,
        client_ip=client_ip,
        sessions=sessions,
        limiter=limiter,
        settings=request.app.state.settings,
        now=clock.now(),
    )
    return _response(tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    body: RefreshRequest,
    request: Request,
    sessions: SessionStore = Depends(get_session_store),
    clock: Clock = Depends(get_clock),
) -> TokenResponse:
    """Spend a refresh token for a new access token and a new refresh token.

    Needs no access token: refreshing is what a client does once its access token has
    expired. A spent refresh token presented again ends the session.
    """
    tokens = await refresh(
        body.refresh_token, sessions=sessions,
        settings=request.app.state.settings, now=clock.now(),
    )
    return _response(tokens)


@router.post("/logout", response_model=LogoutResponse)
async def logout_this_session(
    claims: TokenClaims = Depends(require_role(*ANY_ROLE)),
    sessions: SessionStore = Depends(get_session_store),
) -> LogoutResponse:
    """End the session this access token belongs to. Its refresh token dies with it."""
    await logout(claims.session_id, sessions=sessions)
    return LogoutResponse(sessions_ended=1)


@router.post("/logout-all", response_model=LogoutResponse)
async def logout_all_sessions(
    claims: TokenClaims = Depends(require_role(*ANY_ROLE)),
    sessions: SessionStore = Depends(get_session_store),
) -> LogoutResponse:
    """End every session of the caller's login, on every device."""
    ended = await logout_everywhere(claims.subject, sessions=sessions)
    return LogoutResponse(sessions_ended=ended)


@jwks_router.get("/.well-known/jwks.json", response_model=JwksResponse)
async def jwks(request: Request) -> JwksResponse:
    """The public key access tokens are signed with (RFC 7517).

    For the Part B services, which verify tokens themselves instead of calling Part A on
    every request. Public by design: it can verify a signature and nothing else.
    """
    return JwksResponse(keys=[public_jwk(request.app.state.settings)])


#: A directory lookup is bounded: a page of usage rows, not a dump of every login.
MAX_LOOKUP = 200


@users_router.get("", response_model=list[UserDirectoryEntry])
async def lookup_users(
    ids: list[uuid.UUID] = Query(min_length=1, max_length=MAX_LOOKUP),
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> list[UserDirectoryEntry]:
    """Name, email and role for the given login ids -- admin only.

    For pages that hold user ids from elsewhere, like the assistant's usage page (its
    records carry only the token's subject): the admin's browser joins them, so the
    assistant never reads Part A's users. Unknown ids are simply absent from the answer.
    """
    users = (await session.scalars(select(User).where(User.id.in_(ids)))).all()
    found = [u.id for u in users]
    names = {**await patient_names(session, found), **await provider_names(session, found)}
    return [UserDirectoryEntry(id=u.id, email=u.email, role=u.role.value,
                               is_active=u.is_active, name=names.get(u.id))
            for u in users]
