"""GET /appointments: role gates, patient scoping and query validation, with the service
and the session faked. The SQL -- filters, ordering, totals -- is T3's."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.deps import get_session
from app.main import app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.modules.scheduling import router as scheduling_router
from app.modules.scheduling import service
from app.modules.scheduling.models import AppointmentStatus
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()
CALLER = "11111111-1111-1111-1111-111111111111"
OWN = uuid.uuid4()
NOW = datetime.now(UTC)
STAFF = (UserRole.PROVIDER, UserRole.FRONT_DESK, UserRole.ADMIN)


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=CALLER, role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(monkeypatch):
    state: dict = {"owner": OWN, "calls": []}

    async def _session():
        yield object()

    async def own_patient_id(session, claims):
        return state["owner"]

    async def list_appointments(session, **kwargs):
        state["calls"].append(kwargs)
        row = SimpleNamespace(
            id=uuid.uuid4(), status=AppointmentStatus.CONFIRMED, patient_id=OWN,
            provider_id=uuid.uuid4(), clinic_id=uuid.uuid4(), slot_id=uuid.uuid4(),
            reason=None, confirmed_at=NOW, created_at=NOW,
        )
        return [(row, NOW)], 1

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(scheduling_router, "_own_patient_id", own_patient_id)
    monkeypatch.setattr(service, "list_appointments", list_appointments)
    yield state
    app.dependency_overrides.pop(get_session, None)


async def test_listing_requires_a_token(api_client, world):
    assert (await api_client.get("/appointments")).status_code == 401
    assert world["calls"] == []


@pytest.mark.parametrize("role", [UserRole.PATIENT, *STAFF])
async def test_every_role_may_list_and_the_slot_time_is_returned(api_client, world, role):
    response = await api_client.get("/appointments", headers=auth(role))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["starts_at"] is not None


async def test_a_patient_is_forced_to_their_own_record(api_client, world):
    response = await api_client.get("/appointments", headers=auth(UserRole.PATIENT))
    assert response.status_code == 200
    assert world["calls"][0]["patient_id"] == OWN


async def test_a_patient_naming_their_own_id_is_accepted(api_client, world):
    response = await api_client.get(
        f"/appointments?patient_id={OWN}", headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 200
    assert world["calls"][0]["patient_id"] == OWN


async def test_a_patient_naming_another_patient_is_a_403(api_client, world):
    response = await api_client.get(
        f"/appointments?patient_id={uuid.uuid4()}", headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 403
    assert world["calls"] == []


async def test_a_patient_with_no_record_is_a_404(api_client, world):
    world["owner"] = None
    response = await api_client.get("/appointments", headers=auth(UserRole.PATIENT))
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"
    assert world["calls"] == []


async def test_staff_filters_reach_the_service_and_may_omit_the_patient(api_client, world):
    provider, patient = uuid.uuid4(), uuid.uuid4()
    response = await api_client.get(
        "/appointments",
        params={
            "patient_id": str(patient), "provider_id": str(provider), "status": "cancelled",
            "from": "2026-11-01T00:00:00+00:00", "to": "2026-11-02T00:00:00+00:00",
        },
        headers=auth(UserRole.FRONT_DESK),
    )
    assert response.status_code == 200
    call = world["calls"][0]
    assert (call["patient_id"], call["provider_id"]) == (patient, provider)
    assert call["status"] is AppointmentStatus.CANCELLED
    assert call["start"].tzinfo is not None and call["end"].tzinfo is not None

    await api_client.get("/appointments", headers=auth(UserRole.ADMIN))
    assert world["calls"][1]["patient_id"] is None


@pytest.mark.parametrize("bound", ["from", "to"])
async def test_a_timestamp_without_an_offset_is_rejected(api_client, world, bound):
    response = await api_client.get(
        "/appointments", params={bound: "2026-11-01T10:00:00"},
        headers=auth(UserRole.FRONT_DESK),
    )
    assert response.status_code == 422


async def test_an_unknown_status_is_rejected(api_client, world):
    response = await api_client.get(
        "/appointments?status=pending_failed", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 422


async def test_the_list_does_not_shadow_the_single_read(api_client, world):
    """`""` and `"/{id}"` are distinct routes; a malformed id is still a 422."""
    response = await api_client.get("/appointments/not-a-uuid", headers=auth(UserRole.ADMIN))
    assert response.status_code == 422
