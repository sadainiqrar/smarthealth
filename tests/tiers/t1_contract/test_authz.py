from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.api.error_handlers import register_error_handlers
from app.modules.identity.deps import get_token_settings, require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims, create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings(jwt_secret="authz-test-secret-at-least-32-bytes-long", jwt_expiry_minutes=30)
#: Anchored to the real clock: `decode_access_token` validates `exp` against wall time,
#: so a hardcoded date makes these tests fail forever once that hour passes.
NOW = datetime.now(UTC)


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/admin-only")
    async def admin_only(
        claims: TokenClaims = Depends(require_role(UserRole.ADMIN)),
    ) -> dict[str, str]:
        return {"subject": claims.subject}

    @app.get("/staff")
    async def staff(
        claims: TokenClaims = Depends(
            require_role(UserRole.ADMIN, UserRole.FRONT_DESK)
        ),
    ) -> dict[str, str]:
        return {"subject": claims.subject}

    # Without this the throwaway app diverges from the real one: `require_role` raises
    # `DomainError` subclasses, and an app with no handler registered for them returns
    # nothing useful. These tests asserted status codes alone and so passed anyway,
    # which is exactly how R-6 survived — the 401/403 bodies were never looked at.
    register_error_handlers(app)
    app.dependency_overrides[get_token_settings] = lambda: SETTINGS
    return app


def token_for(role: UserRole) -> str:
    return create_access_token(subject="user-1", role=role, settings=SETTINGS, now=NOW)


async def call(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers=headers or {})


async def test_a_matching_role_is_allowed():
    response = await call(
        "/admin-only", {"Authorization": f"Bearer {token_for(UserRole.ADMIN)}"}
    )
    assert response.status_code == 200
    assert response.json()["subject"] == "user-1"


async def test_any_of_several_allowed_roles_is_accepted():
    response = await call(
        "/staff", {"Authorization": f"Bearer {token_for(UserRole.FRONT_DESK)}"}
    )
    assert response.status_code == 200


async def test_a_wrong_role_is_forbidden_not_unauthorised():
    """403 not 401: the caller proved who they are, they just may not do this."""
    response = await call(
        "/admin-only", {"Authorization": f"Bearer {token_for(UserRole.PATIENT)}"}
    )
    assert response.status_code == 403
    assert response.json()["error"] == "PermissionDenied"


async def test_a_missing_header_is_unauthorised():
    response = await call("/admin-only")
    assert response.status_code == 401


async def test_a_non_bearer_scheme_is_unauthorised():
    response = await call("/admin-only", {"Authorization": "Basic abc123"})
    assert response.status_code == 401


async def test_a_garbage_token_is_unauthorised():
    response = await call("/admin-only", {"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        (None, 401),
        ({"Authorization": "Basic abc123"}, 401),
        ({"Authorization": "Bearer not-a-jwt"}, 401),
    ],
)
async def test_every_rejection_uses_the_same_error_envelope(headers, expected_status):
    """Risk R-6, closed.

    These two statuses used to be the only failures in the system answering
    `{"detail": ...}` with no `error` key, because `require_role` raised
    `HTTPException` and Starlette handles that itself rather than routing it through
    `register_error_handlers`. A client parsing errors uniformly broke on precisely
    the responses it meets most often.
    """
    response = await call("/admin-only", headers)
    assert response.status_code == expected_status
    body = response.json()
    assert set(body) == {"error", "detail"}
    assert body["error"] == "InvalidCredentials"
    assert body["detail"] == "missing or invalid credentials"


async def test_a_401_still_carries_the_challenge_header():
    """RFC 9110 15.5.2 requires it, and routing through `DomainError` must not lose it.

    This is why `DomainError` grew a `headers` attribute rather than the uniform body
    being bought by dropping the header.
    """
    response = await call("/admin-only")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_require_role_needs_at_least_one_role():
    """An empty allow-list would admit nobody while looking like a guard."""
    with pytest.raises(ValueError, match="at least one role"):
        require_role()
