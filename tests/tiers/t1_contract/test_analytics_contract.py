"""GET /analytics/*: who may read the numbers, and which windows are refused, before
any query runs."""

import uuid

import pytest

from app.modules.identity.models import UserRole
from tests.harness.auth import bearer, issue_token

pytestmark = pytest.mark.contract


def _as(role: UserRole) -> dict[str, str]:
    return bearer(issue_token(subject=str(uuid.uuid4()), role=role))


@pytest.mark.parametrize("path", ["/analytics/summary", "/analytics/timeseries?metric=booked"])
async def test_the_numbers_need_a_token(api_client, path):
    assert (await api_client.get(path)).status_code == 401


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.PROVIDER])
async def test_patients_and_providers_may_not_read_clinic_wide_numbers(api_client, role):
    response = await api_client.get("/analytics/summary", headers=_as(role))
    assert response.status_code == 403


async def test_a_window_that_ends_before_it_starts_is_refused(api_client):
    response = await api_client.get(
        "/analytics/summary?from=2026-11-10&to=2026-11-01", headers=_as(UserRole.ADMIN)
    )
    assert response.status_code == 422 and response.json()["error"] == "Unprocessable"


async def test_a_window_longer_than_a_year_is_refused(api_client):
    response = await api_client.get(
        "/analytics/summary?from=2024-01-01&to=2026-01-01", headers=_as(UserRole.FRONT_DESK)
    )
    assert response.status_code == 422


async def test_an_unknown_metric_is_refused(api_client):
    response = await api_client.get(
        "/analytics/timeseries?metric=happiness", headers=_as(UserRole.ADMIN)
    )
    assert response.status_code == 422
