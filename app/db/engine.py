"""Async engine construction.

`create_async_engine` is deliberately lazy — it builds a connection pool without
connecting. That is what lets the T1 contract lane run the application's real
lifespan handler with no database present.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.postgres_dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
        future=True,
    )
