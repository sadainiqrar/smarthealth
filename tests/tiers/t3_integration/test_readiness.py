import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
def live_infrastructure(monkeypatch, db_settings: Settings):
    """Point the application at this run's slice of the live stack."""
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_HOST", db_settings.postgres_host)
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_PORT", str(db_settings.postgres_port))
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_USER", db_settings.postgres_user)
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_PASSWORD", db_settings.postgres_password)
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_DB", db_settings.postgres_db)
    monkeypatch.setenv("SMARTHEALTH_MONGO_HOST", db_settings.mongo_host)
    monkeypatch.setenv("SMARTHEALTH_MONGO_PORT", str(db_settings.mongo_port))
    monkeypatch.setenv("SMARTHEALTH_MONGO_DB", db_settings.mongo_db)
    monkeypatch.setenv("SMARTHEALTH_REDIS_HOST", db_settings.redis_host)
    monkeypatch.setenv("SMARTHEALTH_REDIS_PORT", str(db_settings.redis_port))
    monkeypatch.setenv("SMARTHEALTH_REDIS_DB", str(db_settings.redis_db))


async def test_ready_reports_all_dependencies_healthy(live_infrastructure):
    """Referenced by tests/cases/sys-002-ready-reports-all-dependencies-healthy.yaml."""
    async with LifespanManager(create_app()) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    reported = {entry["name"]: entry["ok"] for entry in body["dependencies"]}
    assert reported == {"postgres": True, "mongo": True, "redis": True}
    assert all(entry["detail"] is None for entry in body["dependencies"])
