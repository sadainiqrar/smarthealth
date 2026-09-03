from datetime import UTC, datetime

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

#: Constructed with no overrides so it resolves to exactly what the running app reads.
#: Hardcoding the secret here would silently break the moment the default changes.
SETTINGS = Settings()
NOW = datetime.now(UTC)

VALID_BODY = {"mrn": "MRN-AUTHZ", "first_name": "Jo", "last_name": "Bloggs"}


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject="11111111-1111-1111-1111-111111111111",
        role=role, settings=SETTINGS, now=NOW,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_registering_a_patient_requires_a_token(api_client):
    response = await api_client.post("/patients", json=VALID_BODY)
    assert response.status_code == 401


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.PROVIDER])
async def test_a_patient_or_provider_may_not_register_patients(api_client, role):
    """Front-desk staff and admins register patients; clinicians do not."""
    response = await api_client.post("/patients", json=VALID_BODY, headers=auth(role))
    assert response.status_code == 403


@pytest.mark.parametrize("role", [UserRole.PATIENT])
async def test_a_patient_may_not_list_patients(api_client, role):
    response = await api_client.get("/patients", headers=auth(role))
    assert response.status_code == 403


async def test_listing_requires_a_token(api_client):
    response = await api_client.get("/patients")
    assert response.status_code == 401


async def test_updating_requires_a_token(api_client):
    response = await api_client.patch(
        "/patients/11111111-1111-1111-1111-111111111111", json={"phone": "x"}
    )
    assert response.status_code == 401


async def test_a_provider_may_not_update_a_patient(api_client):
    response = await api_client.patch(
        "/patients/11111111-1111-1111-1111-111111111111",
        json={"phone": "x"}, headers=auth(UserRole.PROVIDER),
    )
    assert response.status_code == 403


async def test_an_invalid_body_is_rejected_before_authorisation_matters(api_client):
    """422 for a bad body even with a permitted role — validation is not a bypass."""
    response = await api_client.post(
        "/patients", json={"first_name": "Jo"}, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422


async def test_an_out_of_range_limit_is_rejected(api_client):
    response = await api_client.get(
        "/patients?limit=500", headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422
