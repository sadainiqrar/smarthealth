"""Domain errors and their HTTP translation.

Services raise these; routers never catch them. That separation is the reason the
service layer exists at all — Week 2's Temporal activities call the same functions and
need an exception they can act on, not an `HTTPException` that only means something to
a web framework.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    """A business-rule failure. Subclasses carry the status they translate to."""

    status_code = 500

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class PermissionDenied(DomainError):
    status_code = 403


class InvalidCredentials(DomainError):
    """Authentication failed. Deliberately says nothing about which part failed."""

    status_code = 401


def register_error_handlers(app: FastAPI) -> None:
    """One handler on the base class covers every subclass.

    Starlette resolves handlers by walking the raised exception's MRO, so this single
    registration catches `NotFound`, `Conflict` and the rest.
    """

    @app.exception_handler(DomainError)
    async def _handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": type(exc).__name__, "detail": exc.detail},
        )
