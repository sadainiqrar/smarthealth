"""HTTP-layer dependency providers.

This is the seam between the framework and the framework-free core: functions here
take a FastAPI `Request` (or `Query` parameters) and hand back a plain object the rest
of the application already knows how to use. It lives in `app.api`, not `app.core`, so
that code which must stay importable without FastAPI — `app.core.audit`,
`app.core.pagination`, and Week 2's Temporal activities that call the same service
functions — never needs to import this module or anything that transitively pulls in
FastAPI or Starlette.

Keeping `get_audit_log` here, rather than next to `AuditLog` in `app.core.audit`, is
what lets a Temporal activity construct an `AuditLog` directly from a Mongo collection
and a clock, with no ASGI stack involved at all. `page_params` is here for the same
reason: a service takes a `PageParams` value, never a `Query`-bound parameter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditLog
from app.core.clock import Clock, get_clock
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, PageParams
from app.db.mongo import get_audit_collection


def get_audit_log(request: Request, clock: Clock = Depends(get_clock)) -> AuditLog:
    """FastAPI dependency. Services receive an `AuditLog`, never the request.

    `clock` is declared as a dependency rather than obtained by calling `get_clock()`
    inline. Only a declared dependency appears in this provider's graph, and only
    something in the graph can be replaced through `app.dependency_overrides` — an
    inline call would make `app.dependency_overrides[get_clock]` silently do nothing
    to audit timestamps, which is the one place Clock injection is observable through
    a real HTTP request. `app.modules.identity.router` declares it the same way.
    """
    settings = request.app.state.settings
    collection = get_audit_collection(request.app.state.mongo, settings)
    return AuditLog(collection, clock)


def page_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> PageParams:
    """FastAPI dependency for the two query parameters."""
    return PageParams(limit=limit, offset=offset)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to this request.

    Lives here rather than beside `create_session_factory` in `app.db.session` for the
    same reason as `get_audit_log`: it takes a `Request`, and keeping it next to the
    factory made that module framework-bound — which in turn made `app.cli`, and every
    other operational entry point that builds a session, transitively import FastAPI.
    `tests/meta/test_import_boundaries.py` caught exactly that and is what moved it.

    Rolls back on an unhandled exception so a failed request cannot leak a dirty
    transaction back into the pool. Services rely on this: `register_patient` raises
    `Conflict` from inside a failed `flush()`, which leaves the session in the
    "pending rollback" state until someone rolls it back.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
