"""Integration-tier fixtures: a migrated, per-run PostgreSQL database.

`Settings(...)` is constructed directly rather than via `get_settings()`. That
accessor is an `lru_cache` singleton, and a session-scoped fixture calling it would
capture a snapshot that survives every per-test cache clear — silently defeating
isolation exactly where this tier depends on it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import create_engine
from app.db.mongo import create_mongo_client, get_audit_collection
from app.db.session import create_session_factory
from app.main import create_app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
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


@pytest.fixture
async def api(db_settings: Settings, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client against an app pointed at this run's database and Mongo.

    Separate from the T1 `api_client` fixture, which uses default settings and needs no
    infrastructure. This one drives the real stack, so it is what actually exercises
    `get_audit_log`.

    The env is patched rather than `app.state` assigned after the fact because
    `lifespan` builds every client from `get_settings()`, and `require_role` reads the
    same singleton through `get_token_settings`. The autouse `_reset_settings_cache`
    fixture in `tests/conftest.py` clears the `lru_cache` around every test, so the
    patched values are what both of them see.
    """
    for key, value in {
        "SMARTHEALTH_POSTGRES_HOST": db_settings.postgres_host,
        "SMARTHEALTH_POSTGRES_PORT": str(db_settings.postgres_port),
        "SMARTHEALTH_POSTGRES_USER": db_settings.postgres_user,
        "SMARTHEALTH_POSTGRES_PASSWORD": db_settings.postgres_password,
        "SMARTHEALTH_POSTGRES_DB": db_settings.postgres_db,
        "SMARTHEALTH_MONGO_HOST": db_settings.mongo_host,
        "SMARTHEALTH_MONGO_PORT": str(db_settings.mongo_port),
        "SMARTHEALTH_MONGO_DB": db_settings.mongo_db,
        "SMARTHEALTH_REDIS_HOST": db_settings.redis_host,
        "SMARTHEALTH_REDIS_PORT": str(db_settings.redis_port),
        "SMARTHEALTH_REDIS_DB": str(db_settings.redis_db),
    }.items():
        monkeypatch.setenv(key, value)

    # Keep a reference to the FastAPI instance: `LifespanManager.app` is a wrapped
    # ASGI callable, not the application, so it carries no `.state`.
    application = create_app()
    async with LifespanManager(application) as manager:
        # An env var whose name does not match a `Settings` field alias is ignored
        # silently, and the app would then run against the *default* database while
        # the assertions read this run's — indistinguishable from data loss. Fail
        # here instead, where the cause is obvious.
        live = application.state.settings
        assert live.postgres_db == db_settings.postgres_db
        assert live.postgres_port == db_settings.postgres_port
        assert live.mongo_db == db_settings.mongo_db
        assert live.mongo_port == db_settings.mongo_port
        assert live.redis_db == db_settings.redis_db

        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


@pytest.fixture
def token_for(db_settings: Settings):
    """Mint a token the running app will accept, for any role."""

    def _mint(role: UserRole, subject: str | None = None) -> dict[str, str]:
        token = create_access_token(
            subject=subject or str(uuid.uuid4()),
            role=role,
            settings=db_settings,
            now=datetime.now(UTC),
        )
        return {"Authorization": f"Bearer {token}"}

    return _mint


@pytest.fixture
async def audit_documents(db_settings: Settings):
    """Read and clear the audit collection for this run."""
    client = create_mongo_client(db_settings)
    collection = get_audit_collection(client, db_settings)
    await collection.delete_many({})
    try:
        yield collection
    finally:
        await collection.delete_many({})
        await client.close()
