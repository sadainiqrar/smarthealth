import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.settings import Settings

pytestmark = pytest.mark.unit


def test_engine_is_built_from_the_settings_dsn():
    settings = Settings(postgres_host="db.internal", postgres_port=15432, postgres_db="sh")
    engine = create_engine(settings)
    assert isinstance(engine, AsyncEngine)
    assert engine.url.host == "db.internal"
    assert engine.url.port == 15432
    assert engine.url.database == "sh"


def test_creating_an_engine_does_not_connect():
    """The T1 contract lane runs the real lifespan with no database running.

    `create_async_engine` must build a pool without dialing out; if this ever starts
    connecting eagerly, every contract test begins requiring Docker.
    """
    settings = Settings(postgres_host="203.0.113.1", postgres_port=1)
    engine = create_engine(settings)  # must not raise, must not hang
    assert engine.url.port == 1


def test_session_factory_produces_async_sessions():
    settings = Settings()
    factory = create_session_factory(create_engine(settings))
    assert isinstance(factory, sessionmaker)
    assert factory.class_ is AsyncSession


def test_sessions_do_not_expire_objects_on_commit():
    """expire_on_commit=True would re-query attributes after commit, which in async
    code raises MissingGreenlet instead of lazily loading."""
    factory = create_session_factory(create_engine(Settings()))
    assert factory.kw["expire_on_commit"] is False
