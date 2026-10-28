"""GET /patients/me against real Postgres: the record linked to the caller's login."""

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


async def test_a_patient_reads_their_own_record(api, token_for, factory):
    async with factory() as session:
        user = User(
            email=f"me-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("pw-not-used"),
            role=UserRole.PATIENT,
        )
        session.add(user)
        await session.flush()
        patient = Patient(
            mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe",
            user_id=user.id,
        )
        session.add(patient)
        await session.commit()
        user_id, patient_id = user.id, patient.id
    try:
        response = await api.get(
            "/patients/me", headers=token_for(UserRole.PATIENT, subject=str(user_id))
        )
        assert response.status_code == 200
        assert response.json()["id"] == str(patient_id)
    finally:
        async with factory() as session:
            await session.execute(delete(Patient).where(Patient.id == patient_id))
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()


async def test_a_login_with_no_linked_record_is_a_404(api, token_for):
    response = await api.get("/patients/me", headers=token_for(UserRole.PATIENT))
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"
