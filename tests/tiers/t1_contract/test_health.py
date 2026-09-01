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
