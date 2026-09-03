from datetime import UTC, datetime

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

#: Constructed with no overrides so it resolves to exactly what the running app reads.
#: Hardcoding the secret here would silently break the moment the default changes.
SETTINGS = Settings()
#: Anchored to the real clock: `decode_access_token` validates `exp` against wall time,
#: so a hardcoded date makes these tests fail forever once that moment passes.
NOW = datetime.now(UTC)

VALID_BODY = {
    "first_name": "Ada", "last_name": "Lovelace",
    "specialty": "cardiology", "license_number": "LIC-AUTHZ",
}
SOME_ID = "11111111-1111-1111-1111-111111111111"


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=SOME_ID, role=role, settings=SETTINGS, now=NOW
    )
    return {"Authorization": f"Bearer {token}"}


async def test_registering_a_provider_requires_a_token(api_client):
    response = await api_client.post("/providers", json=VALID_BODY)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK]
)
async def test_only_an_admin_may_register_a_provider(api_client, role):
    """Front-desk staff register patients, not clinicians."""
    response = await api_client.post("/providers", json=VALID_BODY, headers=auth(role))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK]
)
async def test_only_an_admin_may_update_a_provider(api_client, role):
    response = await api_client.patch(
        f"/providers/{SOME_ID}", json={"specialty": "neurology"}, headers=auth(role)
    )
    assert response.status_code == 403


async def test_any_authenticated_role_may_list_providers(api_client):
    """A patient may look up who they can be seen by.

    Asserted with an out-of-range `limit` on purpose. A permitted role passes the
    authz gate and reaches the handler, which would hit a database this tier does
    not have -- and `httpx.ASGITransport` re-raises that rather than returning a
    status, so a plain `GET /providers` errors instead of asserting anything. An
    invalid `limit` fails request validation *before* the handler body runs, so a
    **422 rather than a 403** proves the patient role got past authorisation.
    """
    response = await api_client.get(
        "/providers?limit=500", headers=auth(UserRole.PATIENT)
    )
    assert response.status_code == 422


async def test_listing_providers_rejects_an_anonymous_caller(api_client):
    response = await api_client.get("/providers")
    assert response.status_code == 401


async def test_an_invalid_provider_body_is_rejected(api_client):
    response = await api_client.post(
        "/providers", json={"first_name": "Ada"}, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422


async def test_a_licence_number_cannot_be_patched(api_client):
    """`license_number` is absent from ProviderUpdate, so it is ignored rather than
    applied, leaving no set fields -- which `_at_least_one_field` rejects as 422."""
    response = await api_client.patch(
        f"/providers/{SOME_ID}",
        json={"license_number": "LIC-NEW"},
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 422
