import pytest

pytestmark = pytest.mark.contract


async def test_health_reports_ok(api_client):
    response = await api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "environment" in body


async def test_unknown_route_is_404(api_client):
    response = await api_client.get("/definitely-not-a-route")
    assert response.status_code == 404


async def test_the_development_signing_key_is_refused_in_production(monkeypatch):
    """The development key is public; in production it would let anyone mint a token."""
    from asgi_lifespan import LifespanManager

    from app.main import create_app

    monkeypatch.setenv("SMARTHEALTH_ENVIRONMENT", "production")
    monkeypatch.delenv("SMARTHEALTH_JWT_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="development key"):
        async with LifespanManager(create_app()):
            pass
