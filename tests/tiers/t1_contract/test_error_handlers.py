import httpx
import pytest
from fastapi import FastAPI

from app.api.error_handlers import register_error_handlers
from app.core.errors import Conflict, DomainError, InvalidCredentials, NotFound, PermissionDenied

pytestmark = pytest.mark.contract


class _OutOfBudget(DomainError):
    """Defined here, not in app.core.errors, to prove MRO dispatch: a subclass the
    handler has never heard of is still caught by the single registration on the base
    class, because Starlette resolves handlers by walking the exception's MRO."""

    status_code = 402


def build_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/not-found")
    async def not_found() -> None:
        raise NotFound("patient 123 does not exist")

    @app.get("/conflict")
    async def conflict() -> None:
        raise Conflict("slot already held")

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise PermissionDenied("not your appointment")

    @app.get("/unauthorized")
    async def unauthorized() -> None:
        raise InvalidCredentials("invalid token")

    @app.get("/out-of-budget")
    async def out_of_budget() -> None:
        raise _OutOfBudget("insurance budget exhausted")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("mongodb://user:hunter2@mongo:27017 is unreachable")

    return app


async def call(path: str, *, raise_app_exceptions: bool = True) -> httpx.Response:
    transport = httpx.ASGITransport(
        app=build_app(), raise_app_exceptions=raise_app_exceptions
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_not_found_translates_to_404():
    response = await call("/not-found")
    assert response.status_code == 404
    assert response.json() == {"error": "NotFound", "detail": "patient 123 does not exist"}


async def test_conflict_translates_to_409():
    response = await call("/conflict")
    assert response.status_code == 409
    assert response.json() == {"error": "Conflict", "detail": "slot already held"}


async def test_permission_denied_translates_to_403():
    response = await call("/forbidden")
    assert response.status_code == 403
    assert response.json() == {"error": "PermissionDenied", "detail": "not your appointment"}


async def test_invalid_credentials_translates_to_401():
    response = await call("/unauthorized")
    assert response.status_code == 401
    assert response.json() == {"error": "InvalidCredentials", "detail": "invalid token"}


async def test_a_subclass_unknown_to_the_handler_is_still_caught_via_mro():
    """The single handler is registered on DomainError, not on each subclass. This
    proves Starlette actually dispatches by walking the MRO rather than requiring each
    concrete error type to be registered individually."""
    response = await call("/out-of-budget")
    assert response.status_code == 402
    assert response.json() == {
        "error": "_OutOfBudget",
        "detail": "insurance budget exhausted",
    }


async def test_an_unexpected_exception_keeps_the_documented_error_shape():
    """A failure nobody anticipated must still answer `{"error", "detail"}`.

    Starlette's own 500 answers `{"detail": "Internal Server Error"}` with no `error`
    key, so a client parsing errors uniformly would break on exactly the failure that
    is hardest to reproduce.

    `raise_app_exceptions=False` is required to observe this at all:
    `ServerErrorMiddleware` sends the handler's response and then re-raises, so the
    default transport reports the exception instead of the response. That re-raise is
    also why registering this handler did not change any existing test.
    """
    response = await call("/boom", raise_app_exceptions=False)
    assert response.status_code == 500
    assert response.json() == {
        "error": "InternalError",
        "detail": "an unexpected error occurred",
    }


async def test_an_unexpected_exception_leaks_neither_its_text_nor_a_traceback():
    """The message of a real failure carries connection strings and row data."""
    response = await call("/boom", raise_app_exceptions=False)
    body = response.text
    assert "hunter2" not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body


async def test_a_starlette_http_exception_is_not_swallowed_by_the_catch_all():
    """Registering a handler for `Exception` must not change FastAPI's own errors.

    Starlette dispatches `HTTPException` through a different path, so a 404 for an
    unrouted URL keeps its `{"detail": ...}` shape rather than becoming a 500.
    """
    response = await call("/no-such-route")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
