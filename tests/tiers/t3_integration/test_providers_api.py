"""Provider management against real Postgres and real Mongo."""

import uuid

import pytest

from app.modules.identity.models import UserRole

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _body(**overrides) -> dict:
    body = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "specialty": "cardiology",
        "license_number": f"LIC-{uuid.uuid4().hex[:10]}",
    }
    body.update(overrides)
    return body


async def test_admin_registers_a_provider(api, token_for, audit_documents):
    """Referenced by tests/cases/prv-001-admin-registers-a-provider.yaml."""
    response = await api.post(
        "/providers", json=_body(), headers=token_for(UserRole.ADMIN)
    )
    assert response.status_code == 201
    created = response.json()
    assert created["is_active"] is True

    stored = await audit_documents.find_one({"entity_id": created["id"]})
    assert stored is not None, "registering a provider wrote no audit document"
    assert stored["entity_type"] == "provider"
    assert stored["after"]["specialty"] == "cardiology"


async def test_a_duplicate_licence_number_is_a_conflict(api, token_for):
    body = _body()
    headers = token_for(UserRole.ADMIN)
    assert (await api.post("/providers", json=body, headers=headers)).status_code == 201

    second = await api.post("/providers", json=body, headers=headers)
    assert second.status_code == 409
    assert body["license_number"] in second.json()["detail"]


async def test_filtering_by_specialty(api, token_for):
    headers = token_for(UserRole.ADMIN)
    marker = uuid.uuid4().hex[:8]
    await api.post("/providers", json=_body(specialty=f"cardio{marker}"), headers=headers)
    await api.post("/providers", json=_body(specialty=f"neuro{marker}"), headers=headers)

    page = (
        await api.get(f"/providers?specialty=cardio{marker}", headers=headers)
    ).json()
    assert page["total"] == 1
    assert page["items"][0]["specialty"] == f"cardio{marker}"


async def test_a_specialty_of_percent_matches_nothing(api, token_for):
    """`specialty` is case-insensitive equality, never an ILIKE.

    An unescaped ILIKE here would let any authenticated caller pass `%` and page
    through the entire provider directory. Pinned only by compiled SQL until now, so
    prove it against a database that definitely holds at least one provider.
    """
    headers = token_for(UserRole.ADMIN)
    marker = uuid.uuid4().hex[:8]
    assert (
        await api.post("/providers", json=_body(specialty=f"onco{marker}"), headers=headers)
    ).status_code == 201

    everything = (await api.get("/providers", headers=headers)).json()
    assert everything["total"] >= 1

    wildcard = (await api.get("/providers?specialty=%25", headers=headers)).json()
    assert wildcard["total"] == 0, "'%' was interpreted as a wildcard, not a literal"
    assert wildcard["items"] == []


async def test_a_patient_may_read_providers_but_not_create_them(api, token_for):
    """A patient needs to know who they can be seen by."""
    patient_headers = token_for(UserRole.PATIENT)
    assert (await api.get("/providers", headers=patient_headers)).status_code == 200
    assert (
        await api.post("/providers", json=_body(), headers=patient_headers)
    ).status_code == 403


async def test_reading_a_provider_requires_a_token(api, token_for):
    """The detail route's gate is otherwise unproven -- deleting `require_role` from it
    would leave every other test green. It cannot be pinned at the contract tier: an
    anonymous caller with a malformed UUID gets 422 either way, and a well-formed one
    reaches a database that tier does not have."""
    created = (
        await api.post("/providers", json=_body(), headers=token_for(UserRole.ADMIN))
    ).json()

    anonymous = await api.get(f"/providers/{created['id']}")
    assert anonymous.status_code == 401

    permitted = await api.get(
        f"/providers/{created['id']}", headers=token_for(UserRole.PATIENT)
    )
    assert permitted.status_code == 200
    assert permitted.json()["id"] == created["id"]


async def test_deactivating_a_provider_is_audited(api, token_for, audit_documents):
    headers = token_for(UserRole.ADMIN)
    created = (await api.post("/providers", json=_body(), headers=headers)).json()

    updated = (
        await api.patch(
            f"/providers/{created['id']}", json={"is_active": False}, headers=headers
        )
    ).json()
    assert updated["is_active"] is False

    events = await audit_documents.find(
        {"entity_id": created["id"], "action": "profile_updated"}
    ).to_list(length=10)
    assert len(events) == 1
    assert events[0]["before"]["is_active"] is True
    assert events[0]["after"]["is_active"] is False
