import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app

pytestmark = pytest.mark.contract


@pytest.fixture
def unreachable_infrastructure(monkeypatch):
    """Point every dependency at a closed port so readiness must report failure."""
    for variable in ("POSTGRES", "MONGO", "REDIS"):
        monkeypatch.setenv(f"SMARTHEALTH_{variable}_HOST", "127.0.0.1")
        monkeypatch.setenv(f"SMARTHEALTH_{variable}_PORT", "1")


async def test_ready_reports_503_when_dependencies_are_down(unreachable_infrastructure):
    """Readiness must fail loudly rather than optimistically report ok."""
    async with LifespanManager(create_app()) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    reported = {entry["name"]: entry["ok"] for entry in body["dependencies"]}
    assert reported == {"postgres": False, "mongo": False, "redis": False}
    assert all(entry["detail"] for entry in body["dependencies"])


async def test_health_stays_a_flat_liveness_probe(api_client):
    """/health must not grow dependency checks: its dict[str, str] annotation is an
    enforced response model, and it answers 'is the process alive', not 'is it useful'."""
    response = await api_client.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "environment", "service"}
    assert all(isinstance(value, str) for value in response.json().values())


async def test_lifespan_starts_with_no_infrastructure(unreachable_infrastructure):
    """The whole T1 lane depends on startup not dialing out.

    Asserts against the FastAPI instance itself, not `manager.app`: asgi-lifespan
    2.x wraps the given app in a plain ASGI callable (`state_middleware`) so it can
    inject the ASGI-level `scope["state"]` extension, which loses the `.state`
    attribute FastAPI instances carry. The lifespan handler still mutates the
    original instance's `.state` in place, so holding onto it is what works.
    """
    application = create_app()
    async with LifespanManager(application):
        assert application.state.engine is not None
        assert application.state.session_factory is not None
