"""Password hashing, access tokens and the public signing key.

Pure functions over explicit inputs -- no global settings, no implicit clock -- so expiry
and tampering can be tested without patching anything.

Access tokens are EdDSA (Ed25519) JWTs (Weeks 3-5 spec D-25). Asymmetric, so a service
that only needs to *verify* a token -- the Part B services -- is given the public key
through `GET /.well-known/jwks.json` and never holds anything that could sign one. Every
token names its session (`sid`): the signature says who issued the token, the session
store says whether it may still be used (`app/modules/identity/sessions.py`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.modules.identity.models import UserRole
from app.settings import Settings

_password_hash = PasswordHash.recommended()
_logger = logging.getLogger(__name__)

ALGORITHM = "EdDSA"


class InvalidToken(Exception):
    """The token is missing, malformed, expired, not ours, or names no session."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    role: UserRole
    session_id: str


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against a stored hash.

    An unparseable stored hash counts as a failed verification, not an exception: a
    corrupted column must not turn a login into a 500 that leaks a stack trace and
    signals to an attacker that this account differs from the others. The corruption
    is logged so it stays visible.
    """
    try:
        return _password_hash.verify(password, hashed)
    except UnknownHashError:
        _logger.warning("stored password hash is unparseable; treating as a failed login")
        return False


# --- keys -----------------------------------------------------------------------------


def generate_signing_key() -> str:
    """A fresh Ed25519 private key as PKCS#8 PEM, for `SMARTHEALTH_JWT_PRIVATE_KEY`."""
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


@lru_cache(maxsize=8)
def _private_key(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("jwt_private_key must be an Ed25519 private key")
    return key


def _public_key(settings: Settings) -> Ed25519PublicKey:
    return _private_key(settings.jwt_private_key).public_key()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def public_jwk(settings: Settings) -> dict[str, str]:
    """The verification key as a JWK (RFC 8037), its `kid` the RFC 7638 thumbprint.

    The thumbprint makes the id a function of the key itself: rotating the key rotates
    the id, and two services can never disagree about which key an id names.
    """
    raw = _public_key(settings).public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    members = {"crv": "Ed25519", "kty": "OKP", "x": _b64url(raw)}
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
    kid = _b64url(hashlib.sha256(canonical).digest())
    return {**members, "kid": kid, "alg": ALGORITHM, "use": "sig"}


# --- tokens ---------------------------------------------------------------------------


def create_access_token(
    *,
    subject: str,
    role: UserRole,
    session_id: str,
    settings: Settings,
    now: datetime,
    expires_in: timedelta | None = None,
) -> str:
    if now.tzinfo is None:
        raise ValueError(
            "`now` must be timezone-aware: datetime.timestamp() reads a naive value as "
            "local time, which silently shifts the token's lifetime by the UTC offset"
        )
    lifetime = expires_in or timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": subject,
        "role": role.value,
        "sid": session_id,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(
        payload,
        _private_key(settings.jwt_private_key),
        algorithm=ALGORITHM,
        headers={"kid": public_jwk(settings)["kid"]},
    )


def decode_access_token(token: str, *, settings: Settings) -> TokenClaims:
    try:
        # verify_iat=False: PyJWT checks "iat" against the real wall clock with zero
        # leeway. That's fragile against ordinary clock skew between the machine that
        # issued the token and the machine validating it — a few seconds of drift
        # would reject a perfectly good token. Token lifetime is controlled by "exp",
        # not "iat", and "exp" is still fully enforced below.
        payload = jwt.decode(
            token,
            _public_key(settings),
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            options={"verify_iat": False, "require": ["exp", "iss", "sub", "sid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("token has expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"token is not valid: {exc}") from exc

    subject = payload.get("sub")
    raw_role = payload.get("role")
    session_id = payload.get("sid")
    if not subject or not raw_role or not session_id:
        raise InvalidToken("token is missing 'sub', 'role' or 'sid'")
    try:
        role = UserRole(raw_role)
    except ValueError as exc:
        raise InvalidToken(f"token carries an unknown role '{raw_role}'") from exc
    return TokenClaims(subject=subject, role=role, session_id=str(session_id))
