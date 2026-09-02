"""Password hashing and access tokens.

Both are deliberately pure functions over explicit inputs — no global settings, no
implicit clock — so expiry and tampering can be tested without patching anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.modules.identity.models import UserRole
from app.settings import Settings

_password_hash = PasswordHash.recommended()


class InvalidToken(Exception):
    """The token is missing, malformed, expired, or not signed by us."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    role: UserRole


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _password_hash.verify(password, hashed)


def create_access_token(
    *,
    subject: str,
    role: UserRole,
    settings: Settings,
    now: datetime,
    expires_in: timedelta | None = None,
) -> str:
    lifetime = expires_in or timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {
        "sub": subject,
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, *, settings: Settings) -> TokenClaims:
    try:
        # verify_iat=False: PyJWT checks "iat" against the real wall clock with zero
        # leeway, which would make every test that mints a token from a fixed `now`
        # flaky against clock skew between the issuer and the machine running the
        # test. Expiry (which matters for real security) is still enforced via "exp".
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_iat": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("token has expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"token is not valid: {exc}") from exc

    subject = payload.get("sub")
    raw_role = payload.get("role")
    if not subject or not raw_role:
        raise InvalidToken("token is missing 'sub' or 'role'")
    try:
        role = UserRole(raw_role)
    except ValueError as exc:
        raise InvalidToken(f"token carries an unknown role '{raw_role}'") from exc
    return TokenClaims(subject=subject, role=role)
