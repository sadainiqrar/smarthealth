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

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import DomainError

logger = logging.getLogger(__name__)

#: What a client sees when something we did not anticipate went wrong. Deliberately
#: says nothing: `str(exc)` on an unexpected failure routinely carries a DSN with
#: credentials (asyncpg, pymongo) or the row data that broke a constraint.
INTERNAL_ERROR_BODY = {"error": "InternalError", "detail": "an unexpected error occurred"}


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

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Keep `{"error", "detail"}` as the shape of *every* failure.

        Without this, an unhandled exception — Mongo unreachable during an audit
        write, say — falls through to Starlette's own 500, which answers
        `{"detail": "Internal Server Error"}` and no `error` key. A client parsing
        errors uniformly then breaks on precisely the failure that is hardest to
        reproduce.

        Two things about this registration are not obvious:

        1. Starlette pulls the handler for `Exception` out of the normal
           exception-handler map and gives it to `ServerErrorMiddleware`, which sends
           this response and then **re-raises** the exception so the server (or a test
           client) can still see it. So this changes what a client observes, not what
           the process logs — and `httpx.ASGITransport` still raises unless it is
           built with `raise_app_exceptions=False`.
        2. It does not catch `HTTPException`; Starlette handles that separately, so
           FastAPI's 404s and 422s keep their own shapes.

        The exception text never reaches the client (see `INTERNAL_ERROR_BODY`); it
        goes to the log with a traceback instead, where it stays diagnosable.
        """
        logger.exception(
            "unhandled exception serving %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return JSONResponse(status_code=500, content=INTERNAL_ERROR_BODY)
