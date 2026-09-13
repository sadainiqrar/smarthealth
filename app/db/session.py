"""Session factory.

Framework-free by the same rule as `app.core.errors`, `app.core.audit` and
`app.db.constraints`: importing this must not pull FastAPI, Starlette or the ASGI stack
into a process that has no business loading them. `app.cli` builds a session factory to
create a user or seed a database, and Week 2's Temporal activities will do the same from
a worker with no HTTP request anywhere in sight.

The request-scoped `get_session` dependency lives in `app.api.deps`, not here, for
exactly the reason `get_audit_log` does: it takes a FastAPI `Request`, and keeping it
beside the factory made this module framework-bound and every operational entry point
that touched it framework-bound with it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """One session per request.

    `expire_on_commit=False` because expiring attributes after commit triggers a
    lazy reload, which in async SQLAlchemy raises MissingGreenlet rather than
    silently issuing a query.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
