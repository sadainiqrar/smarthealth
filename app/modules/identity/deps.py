"""Authorisation dependencies.

`require_role` is a dependency factory, so a route declares the roles it accepts and
the enforcement happens before the handler body runs. The harness's future
`test_endpoints_authz` meta-test enumerates routes and asserts each declares one.

These raise `DomainError` subclasses rather than `HTTPException` (risk R-6). Starlette
handles `HTTPException` itself, so a 401 or 403 raised that way answered
`{"detail": ...}` with no `error` key while every other failure in the system answered
`{"error", "detail"}` — a client parsing errors uniformly broke on exactly the two
statuses it is most likely to meet. The translation now runs through the same handler
as everything else.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request

from app.core.errors import InvalidCredentials, PermissionDenied
from app.modules.identity.models import UserRole
from app.modules.identity.security import InvalidToken, TokenClaims, decode_access_token
from app.settings import Settings, get_settings

#: Built fresh per raise rather than shared as a module-level singleton: an exception
#: instance accumulates traceback state, and one reused across requests is a leak.
_UNAUTHORISED_DETAIL = "missing or invalid credentials"


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
            raise InvalidCredentials(_UNAUTHORISED_DETAIL)
        try:
            claims = decode_access_token(token, settings=settings)
        except InvalidToken as exc:
            raise InvalidCredentials(_UNAUTHORISED_DETAIL) from exc
        if claims.role not in permitted:
            raise PermissionDenied(
                f"role '{claims.role.value}' may not perform this action; "
                f"requires one of: {', '.join(sorted(r.value for r in permitted))}"
            )
        return claims

    return dependency
