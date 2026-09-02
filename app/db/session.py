"""Session factory and the request-scoped session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """One session per request.

    `expire_on_commit=False` because expiring attributes after commit triggers a
    lazy reload, which in async SQLAlchemy raises MissingGreenlet rather than
    silently issuing a query.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to this request.

    Rolls back on an unhandled exception so a failed request cannot leak a dirty
    transaction into the pool.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
