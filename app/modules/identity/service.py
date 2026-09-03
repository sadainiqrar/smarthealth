"""Authentication.

No FastAPI import: Week 2's Temporal activities call these functions directly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidCredentials
from app.modules.identity.models import User
from app.modules.identity.security import create_access_token, verify_password
from app.settings import Settings


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    settings: Settings,
    now: datetime,
) -> tuple[str, int]:
    """Return an access token and its lifetime in seconds.

    Every failure path raises the same error with the same message. Distinguishing
    "no such account" from "wrong password" tells an attacker which addresses are
    registered, and an inactive account should not be enumerable either.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise InvalidCredentials("email or password is incorrect")

    token = create_access_token(subject=str(user.id), role=user.role, settings=settings, now=now)
    return token, settings.jwt_expiry_minutes * 60
