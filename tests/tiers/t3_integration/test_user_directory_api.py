"""GET /users against real Postgres: the admin's directory lookup behind the AI usage page."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients.models import Patient

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_admin_gets_name_email_and_role_for_the_ids_asked(api, token_for, factory):
    """Referenced by tests/cases/id-010-admin-user-directory.yaml."""
    tag = uuid.uuid4().hex[:8]
    async with factory() as session:
        patient_login = User(email=f"dir-p-{tag}@example.com", role=UserRole.PATIENT,
                             password_hash=hash_password("pw-not-used"))
        staff_login = User(email=f"dir-s-{tag}@example.com", role=UserRole.FRONT_DESK,
                           password_hash=hash_password("pw-not-used"))
        session.add_all([patient_login, staff_login])
        await session.flush()
        session.add(Patient(mrn=f"MRN-{tag}", first_name="Ines", last_name="Novak",
                            user_id=patient_login.id))
        await session.commit()
        ids = [patient_login.id, staff_login.id]
    try:
        unknown = uuid.uuid4()
        response = await api.get(
            "/users", params=[("ids", str(i)) for i in [*ids, unknown]],
            headers=token_for(UserRole.ADMIN))
        assert response.status_code == 200
        found = {row["id"]: row for row in response.json()}
        assert set(found) == {str(i) for i in ids}  # unknown ids are simply absent
        assert found[str(ids[0])] == {"id": str(ids[0]), "email": f"dir-p-{tag}@example.com",
                                      "role": "patient", "is_active": True, "name": "Ines Novak"}
        assert found[str(ids[1])]["name"] is None  # staff with no linked record
        assert "password_hash" not in found[str(ids[1])]
    finally:
        async with factory() as session:
            await session.execute(delete(Patient).where(Patient.mrn == f"MRN-{tag}"))
            await session.execute(delete(User).where(User.id.in_(ids)))
            await session.commit()


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK])
async def test_only_admin_may_look_users_up(api, token_for, role):
    response = await api.get("/users", params={"ids": str(uuid.uuid4())},
                             headers=token_for(role))
    assert response.status_code == 403
