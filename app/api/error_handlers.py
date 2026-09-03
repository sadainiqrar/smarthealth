"""HTTP translation for domain errors.

This is wiring, not domain logic — it lives in the `api` layer, not next to the
exceptions in `app.core.errors`, so that a Temporal activity (or any other non-HTTP
caller of a service function) can import and raise `DomainError` subclasses without
dragging FastAPI, Starlette, or the ASGI stack into its process. Only code that already
depends on FastAPI — routers, `app.main` — should import this module.

Known limitation: this only translates exceptions raised while FastAPI is still
building the response. A `DomainError` raised inside a `StreamingResponse` body, or
inside a `BackgroundTasks` callback, runs after the response has already started and is
not caught here — the client sees a truncated/broken response, not a translated error.
Part B's streaming LLM responses will need their own handling for this.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import DomainError


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
