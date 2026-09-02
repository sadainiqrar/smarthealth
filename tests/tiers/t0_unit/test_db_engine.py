from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine
from app.db.session import create_session_factory, get_session
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
    assert isinstance(factory, async_sessionmaker)
    assert factory.class_ is AsyncSession


def test_sessions_do_not_expire_objects_on_commit():
    """expire_on_commit=True would re-query attributes after commit, which in async
    code raises MissingGreenlet instead of lazily loading."""
    factory = create_session_factory(create_engine(Settings()))
    assert factory.kw["expire_on_commit"] is False


class _SpySession:
    """A stand-in for AsyncSession that records whether rollback was called."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> "_SpySession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self.closed = True
        return False


def _request_with(session: _SpySession) -> SimpleNamespace:
    """The minimum shape `get_session` reads: request.app.state.session_factory."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=lambda: session))
    )


async def test_get_session_yields_a_session_from_app_state():
    session = _SpySession()
    generator = get_session(_request_with(session))
    assert await anext(generator) is session
    await generator.aclose()


async def test_get_session_rolls_back_when_the_request_raises():
    """A failed request must not return a dirty transaction to the pool, where it
    would surface as a corrupt state in whichever request borrows that connection next."""
    session = _SpySession()
    generator = get_session(_request_with(session))
    await anext(generator)

    with pytest.raises(ValueError, match="boom"):
        await generator.athrow(ValueError("boom"))

    assert session.rolled_back
    assert session.closed


async def test_get_session_does_not_roll_back_on_success():
    session = _SpySession()
    generator = get_session(_request_with(session))
    await anext(generator)
    await generator.aclose()

    assert not session.rolled_back
    assert session.closed
