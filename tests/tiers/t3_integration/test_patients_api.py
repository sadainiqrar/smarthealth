"""Patient management against real Postgres and real Mongo.

This is the first tier in which `app.api.deps.get_audit_log` is executed against a
real collection, so the audit assertions here are the point of the file, not
decoration.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from app.modules.identity.models import UserRole

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _body(**overrides) -> dict:
    body = {
        "mrn": f"MRN-{uuid.uuid4().hex[:8]}",
        "first_name": "Jo",
        "last_name": "Bloggs",
    }
    body.update(overrides)
    return body


async def test_front_desk_registers_a_patient(api, token_for, audit_documents):
    """Referenced by tests/cases/pat-001-front-desk-registers-a-patient.yaml.

    A walk-in has no user account -- the requirement that made user_id nullable.
    """
    response = await api.post(
        "/patients", json=_body(), headers=token_for(UserRole.FRONT_DESK)
    )
    assert response.status_code == 201
    created = response.json()
    assert created["user_id"] is None
    assert created["id"]

    stored = await audit_documents.find_one({"entity_id": created["id"]})
    assert stored is not None, "registering a patient wrote no audit document"
    assert stored["action"] == "registered"
    assert stored["entity_type"] == "patient"
    assert stored["before"] is None
    assert stored["after"]["mrn"] == created["mrn"]


async def test_a_duplicate_mrn_is_a_conflict(api, token_for):
    body = _body()
    headers = token_for(UserRole.ADMIN)
    first = await api.post("/patients", json=body, headers=headers)
    assert first.status_code == 201

    second = await api.post("/patients", json=body, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"] == "Conflict"
    assert body["mrn"] in second.json()["detail"]


async def test_reading_an_absent_patient_is_a_404(api, token_for):
    response = await api.get(
        f"/patients/{uuid.uuid4()}", headers=token_for(UserRole.ADMIN)
    )
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


async def test_reading_a_patient_requires_a_token(api, token_for):
    """The detail route's gate is otherwise unproven: `tests/tiers/t1_contract/
    test_patients_authz.py` covers POST, GET (list) and PATCH anonymously, but never
    `GET /patients/{patient_id}`, so `require_role` could be deleted from it and the
    whole suite would stay green. A provider is the interesting permitted role: it may
    read a patient but may not write one."""
    created = (
        await api.post("/patients", json=_body(), headers=token_for(UserRole.FRONT_DESK))
    ).json()

    anonymous = await api.get(f"/patients/{created['id']}")
    assert anonymous.status_code == 401

    permitted = await api.get(
        f"/patients/{created['id']}", headers=token_for(UserRole.PROVIDER)
    )
    assert permitted.status_code == 200
    assert permitted.json()["id"] == created["id"]


async def test_an_update_is_audited_with_before_and_after(api, token_for, audit_documents):
    headers = token_for(UserRole.FRONT_DESK)
    created = (await api.post("/patients", json=_body(), headers=headers)).json()

    response = await api.patch(
        f"/patients/{created['id']}",
        json={"phone": "+44 20 7946 0000"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["phone"] == "+44 20 7946 0000"

    updates = await audit_documents.find(
        {"entity_id": created["id"], "action": "profile_updated"}
    ).to_list(length=10)
    assert len(updates) == 1
    assert updates[0]["before"]["phone"] is None
    assert updates[0]["after"]["phone"] == "+44 20 7946 0000"


async def test_a_partial_update_does_not_erase_unsent_fields(api, token_for):
    """A PATCH that nulled every omitted field would silently destroy data."""
    headers = token_for(UserRole.ADMIN)
    created = (
        await api.post("/patients", json=_body(phone="+1 555 0100"), headers=headers)
    ).json()

    updated = (
        await api.patch(
            f"/patients/{created['id']}", json={"first_name": "Josephine"},
            headers=headers,
        )
    ).json()

    assert updated["first_name"] == "Josephine"
    assert updated["phone"] == "+1 555 0100"


async def test_search_matches_mrn_and_name(api, token_for):
    headers = token_for(UserRole.ADMIN)
    unique = uuid.uuid4().hex[:8]
    await api.post(
        "/patients", json=_body(mrn=f"MRN-{unique}", last_name=f"Zeta{unique}"),
        headers=headers,
    )

    by_mrn = (await api.get(f"/patients?search={unique}", headers=headers)).json()
    assert by_mrn["total"] >= 1

    by_name = (await api.get(f"/patients?search=Zeta{unique}", headers=headers)).json()
    assert by_name["total"] == 1


async def test_search_is_case_insensitive_and_escapes_like_wildcards(api, token_for):
    """`icontains(..., autoescape=True)` -- both halves, against a real database.

    Pinned only by compiled-SQL assertions until now, and the property was got wrong
    twice: `contains` instead of `icontains` makes a lowercase term stop matching a
    capitalised name, and dropping `autoescape` turns a searched `_` into a
    single-character wildcard that silently widens the result set.
    """
    headers = token_for(UserRole.ADMIN)
    unique = uuid.uuid4().hex[:8]
    await api.post(
        "/patients", json=_body(last_name=f"MacDonald{unique}"), headers=headers
    )
    await api.post("/patients", json=_body(last_name=f"a_b{unique}"), headers=headers)
    await api.post("/patients", json=_body(last_name=f"axb{unique}"), headers=headers)

    lowercased = (
        await api.get(f"/patients?search=macdonald{unique}", headers=headers)
    ).json()
    assert lowercased["total"] == 1, "a lowercase term did not match a capitalised name"
    assert lowercased["items"][0]["last_name"] == f"MacDonald{unique}"

    underscore = (await api.get(f"/patients?search=a_b{unique}", headers=headers)).json()
    assert underscore["total"] == 1, "'_' was treated as a single-character wildcard"
    assert underscore["items"][0]["last_name"] == f"a_b{unique}"


async def test_pagination_reports_the_total_and_slices(api, token_for):
    headers = token_for(UserRole.ADMIN)
    marker = uuid.uuid4().hex[:8]
    for index in range(3):
        await api.post(
            "/patients",
            json=_body(mrn=f"MRN-{marker}-{index}", last_name=f"Page{marker}"),
            headers=headers,
        )

    page = (
        await api.get(f"/patients?search=Page{marker}&limit=2&offset=0", headers=headers)
    ).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2

    second = (
        await api.get(f"/patients?search=Page{marker}&limit=2&offset=2", headers=headers)
    ).json()
    assert len(second["items"]) == 1


async def test_a_patch_returns_the_timestamp_the_database_actually_holds(
    api, token_for, db_session
):
    """The guard on `eager_defaults=True` (app/db/base.py).

    `updated_at` has `onupdate=func.now()`, a SQL expression, so SQLAlchemy expires the
    attribute after an UPDATE; without eager fetching the serialiser's lazy reload
    raised `MissingGreenlet` and every PATCH 500'd. `eager_defaults` makes the UPDATE
    carry RETURNING instead -- but a value that came back *stale* would be worse than
    the crash, because nothing would look wrong. Compare it against a fresh read.
    """
    headers = token_for(UserRole.ADMIN)
    created = (await api.post("/patients", json=_body(), headers=headers)).json()

    updated = (
        await api.patch(
            f"/patients/{created['id']}", json={"first_name": "Grace"}, headers=headers
        )
    ).json()

    row = (
        await db_session.execute(
            text("select created_at, updated_at from patients where id = :id"),
            {"id": created["id"]},
        )
    ).one()
    assert datetime.fromisoformat(updated["updated_at"]) == row.updated_at
    # The BEFORE UPDATE trigger fired, so this is not merely the INSERT's value
    # echoed back.
    assert row.updated_at > row.created_at
