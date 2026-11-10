"""Verifying Part A's access tokens without Part A (spec D-25).

The public key comes from Part A's JWKS endpoint, fetched once and cached by key id; a
token naming an unknown `kid` triggers one refetch (Part A rotated its key). Only EdDSA is
accepted, and the issuer must be Part A. This service never sees anything that could sign
a token, and never reads Part A's session store: revocation reaches it as an event
(`identity.sessions_revoked`) in the assistant, and within the ten-minute token lifetime
otherwise.

Copied, not shared, between the Part B services: contracts are files, code is each
service's own (spec section 1.2).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

#: How long a fetched key set is trusted before it is fetched again anyway.
CACHE_SECONDS = 300


class Unauthenticated(Exception):
    """No token, or one this service cannot trust."""


@dataclass(frozen=True)
class Caller:
    user_id: str
    role: str
    session_id: str
    issued_at: int
    token: str

    @property
    def audience(self) -> str:
        """Which corpus the caller may read (spec 8.4): patients see the patient corpus,
        every staff role the staff one too."""
        return "patient" if self.role == "patient" else "staff"


def fetch_jwks(url: str) -> dict[str, Any]:
    response = httpx.get(url, timeout=5)
    response.raise_for_status()
    return response.json()


class TokenVerifier:
    def __init__(self, jwks_url: str, issuer: str, *,
                 fetch: Callable[[str], dict[str, Any]] = fetch_jwks) -> None:
        self._url = jwks_url
        self._issuer = issuer
        self._fetch = fetch
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _key(self, kid: str) -> Any:
        with self._lock:
            stale = time.monotonic() - self._fetched_at > CACHE_SECONDS
            if kid not in self._keys or stale:
                keys = self._fetch(self._url).get("keys", [])
                self._keys = {key["kid"]: jwt.PyJWK(key).key for key in keys}
                self._fetched_at = time.monotonic()
            if kid not in self._keys:
                raise Unauthenticated("token signed with a key Part A does not publish")
            return self._keys[kid]

    def verify(self, authorization: str | None) -> Caller:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("missing bearer token")
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
            claims = jwt.decode(
                token, self._key(kid), algorithms=["EdDSA"], issuer=self._issuer,
                options={"require": ["exp", "iss", "sub", "sid"], "verify_iat": False},
            )
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            raise Unauthenticated(f"token not valid: {exc}") from exc
        return Caller(claims["sub"], claims.get("role", ""), claims["sid"],
                      int(claims.get("iat", 0)), token)
