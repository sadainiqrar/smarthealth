"""GET /monitoring/*: admin only, and a dead source is a 200 that says so."""

import uuid

import pytest

from app.main import app
from app.modules.identity.models import UserRole
from app.modules.monitoring import queries
from app.modules.monitoring.client import Sample
from app.modules.monitoring.router import get_monitoring
from app.modules.monitoring.service import MonitoringService
from tests.harness.auth import bearer, issue_token
from tests.tiers.t0_unit.test_monitoring_service import FakeJaeger, FakePrometheus

pytestmark = pytest.mark.contract


def _as(role: UserRole) -> dict[str, str]:
    return bearer(issue_token(subject=str(uuid.uuid4()), role=role))


def _serve(service: MonitoringService):
    app.dependency_overrides[get_monitoring] = lambda: service


@pytest.fixture
def monitoring():
    service = MonitoringService(FakePrometheus(down=True), FakeJaeger())
    app.dependency_overrides[get_monitoring] = lambda: service
    yield service
    app.dependency_overrides.pop(get_monitoring, None)


@pytest.mark.parametrize("path", ["/monitoring/overview", "/monitoring/failures"])
async def test_the_health_page_needs_a_token(api_client, monitoring, path):
    assert (await api_client.get(path)).status_code == 401


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK])
@pytest.mark.parametrize("path", ["/monitoring/overview", "/monitoring/failures"])
async def test_only_admins_see_system_health(api_client, monitoring, role, path):
    assert (await api_client.get(path, headers=_as(role))).status_code == 403


async def test_prometheus_down_is_a_200_that_says_so(api_client, monitoring):
    response = await api_client.get("/monitoring/overview?hours=24", headers=_as(UserRole.ADMIN))
    assert response.status_code == 200
    body = response.json()
    assert body["sources"]["prometheus"] == "unavailable"
    assert {f["status"] for f in body["flows"]} == {"unknown"}


async def test_failures_answer_with_their_source(api_client, monitoring):
    response = await api_client.get("/monitoring/failures?hours=1&limit=5",
                                    headers=_as(UserRole.ADMIN))
    assert response.status_code == 200
    assert response.json() == {"window_hours": 1, "source": "ok", "failures": []}


@pytest.mark.parametrize("query", ["hours=2", "hours=0", "limit=0", "limit=51"])
async def test_windows_and_limits_outside_the_allowed_set_are_refused(
    api_client, monitoring, query
):
    path = "/monitoring/failures" if "limit" in query else "/monitoring/overview"
    response = await api_client.get(f"{path}?{query}", headers=_as(UserRole.ADMIN))
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/monitoring/overview", "/monitoring/failures"])
async def test_a_patient_with_a_bad_window_is_forbidden_not_told_the_window_is_wrong(
    api_client, monitoring, path
):
    response = await api_client.get(f"{path}?hours=2", headers=_as(UserRole.PATIENT))
    assert response.status_code == 403


async def test_a_working_source_gives_real_figures(api_client, monitoring):
    flow = queries.FLOWS[0]
    answers = {
        queries.HEARTBEAT_AGE: [Sample({"job": job}, 4.0) for job, _ in queries.COMPONENTS],
        queries.flow_total(flow, "24h"): [Sample({}, 40.0)],
        queries.flow_failed(flow, "24h"): [Sample({}, 1.0)],
    }
    _serve(MonitoringService(FakePrometheus(answers), FakeJaeger()))
    response = await api_client.get("/monitoring/overview?hours=24", headers=_as(UserRole.ADMIN))
    assert response.status_code == 200
    body = response.json()
    assert body["sources"] == {"prometheus": "ok", "jaeger": "ok"}
    booking = next(f for f in body["flows"] if f["key"] == "booking")
    assert (booking["total"], booking["failed"], booking["status"]) == (40, 1, "ok")


async def test_jaeger_down_is_a_200_that_says_so(api_client, monitoring):
    _serve(MonitoringService(FakePrometheus(), FakeJaeger(down=True)))
    admin = _as(UserRole.ADMIN)
    response = await api_client.get("/monitoring/failures?hours=24", headers=admin)
    assert response.status_code == 200
    assert response.json()["source"] == "unavailable" and response.json()["failures"] == []
    overview = await api_client.get("/monitoring/overview?hours=24", headers=admin)
    assert overview.json()["sources"]["jaeger"] == "unavailable"


async def test_sources_that_are_not_configured_say_so(api_client, monitoring):
    _serve(MonitoringService(None, None))
    admin = _as(UserRole.ADMIN)
    overview = await api_client.get("/monitoring/overview?hours=24", headers=admin)
    failures = await api_client.get("/monitoring/failures?hours=24", headers=admin)
    assert overview.status_code == failures.status_code == 200
    assert overview.json()["sources"] == {"prometheus": "not_configured",
                                          "jaeger": "not_configured"}
    assert failures.json()["source"] == "not_configured"
