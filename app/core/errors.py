"""Domain errors.

Services raise these; routers never catch them. That separation is the reason the
service layer exists at all — Week 2's Temporal activities call the same functions and
need an exception they can act on, not an `HTTPException` that only means something to
a web framework.

This module has zero framework imports on purpose: importing it must not pull FastAPI,
Starlette, or the ASGI stack into a process that has no business loading them (a
Temporal worker, a Celery task, a plain script). The HTTP translation of these errors
lives in `app.api.error_handlers`, not here.
"""

from __future__ import annotations


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
