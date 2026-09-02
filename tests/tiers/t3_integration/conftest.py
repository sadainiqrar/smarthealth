"""Integration-tier fixtures: a migrated, per-run PostgreSQL database.

`Settings(...)` is constructed directly rather than via `get_settings()`. That
accessor is an `lru_cache` singleton, and a session-scoped fixture calling it would
capture a snapshot that survives every per-test cache clear — silently defeating
isolation exactly where this tier depends on it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.settings import Settings
from tests.harness.db import create_database_sync, drop_database_sync, run_migrations
from tests.harness.isolation import RunIsolation


def _test_stack_settings(isolation: RunIsolation) -> Settings:
    """Point settings at the compose test stack's offset ports for this run's slice."""
    return Settings(
        postgres_host=os.environ.get("SMARTHEALTH_TEST_PG_HOST", "localhost"),
        postgres_port=int(os.environ.get("SMARTHEALTH_TEST_PG_PORT", "15432")),
        postgres_user="smarthealth",
        postgres_password="smarthealth",
        postgres_db=isolation.postgres_db,
        mongo_host="localhost",
        mongo_port=27018,
        mongo_db=isolation.mongo_db,
        redis_host="localhost",
        redis_port=16379,
        redis_db=isolation.redis_db,
        redis_prefix=isolation.redis_prefix,
        resource_prefix=isolation.resource_prefix,
    )


@pytest.fixture(scope="session")
def db_settings(isolation: RunIsolation, stack) -> Iterator[Settings]:
    """A migrated database of this run's own, dropped when the session ends."""
    settings = _test_stack_settings(isolation)
    create_database_sync(settings.postgres_admin_url, settings.postgres_db)
    try:
        run_migrations(
            {
                "SMARTHEALTH_POSTGRES_HOST": settings.postgres_host,
                "SMARTHEALTH_POSTGRES_PORT": str(settings.postgres_port),
                "SMARTHEALTH_POSTGRES_USER": settings.postgres_user,
                "SMARTHEALTH_POSTGRES_PASSWORD": settings.postgres_password,
                "SMARTHEALTH_POSTGRES_DB": settings.postgres_db,
            }
        )
        yield settings
    finally:
        drop_database_sync(settings.postgres_admin_url, settings.postgres_db)


@pytest.fixture
async def db_session(db_settings: Settings) -> AsyncIterator[AsyncSession]:
    """A session on the migrated database. Rolls back so tests cannot bleed."""
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()
