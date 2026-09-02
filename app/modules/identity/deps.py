"""Authorisation dependencies.

`require_role` is a dependency factory, so a route declares the roles it accepts and
the enforcement happens before the handler body runs. The harness's future
`test_endpoints_authz` meta-test enumerates routes and asserts each declares one.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from app.modules.identity.models import UserRole
from app.modules.identity.security import InvalidToken, TokenClaims, decode_access_token
from app.settings import Settings, get_settings

_UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="missing or invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_token_settings() -> Settings:
    """Indirection so tests can override the signing settings for one app."""
    return get_settings()


def require_role(*allowed: UserRole) -> Callable[..., TokenClaims]:
    """Build a dependency that admits only the listed roles.

    401 when identity cannot be established; 403 when it can but the role is wrong —
    the distinction matters to a client deciding whether to re-authenticate.
    """
    if not allowed:
        raise ValueError("require_role needs at least one role")
    permitted = frozenset(allowed)

    def dependency(
        request: Request, settings: Settings = Depends(get_token_settings)
    ) -> TokenClaims:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _UNAUTHORISED
        try:
            claims = decode_access_token(token, settings=settings)
        except InvalidToken as exc:
            raise _UNAUTHORISED from exc
        if claims.role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role '{claims.role.value}' may not perform this action; "
                    f"requires one of: {', '.join(sorted(r.value for r in permitted))}"
                ),
            )
        return claims

    return dependency
