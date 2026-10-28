"""The published patient interface, against real Postgres."""

import uuid

import pytest

from app.core.errors import NotFound
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients import public
from app.modules.patients.models import Patient

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_an_existing_patient_is_bookable(db_session):
    patient = Patient(mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe")
    db_session.add(patient)
    await db_session.flush()
    await public.ensure_bookable_patient(db_session, patient.id)


async def test_an_unknown_patient_is_not_found(db_session):
    with pytest.raises(NotFound):
        await public.ensure_bookable_patient(db_session, uuid.uuid4())


async def test_a_user_resolves_to_their_linked_patient(db_session):
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("x" * 12), role=UserRole.PATIENT,
    )
    db_session.add(user)
    await db_session.flush()
    patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe", user_id=user.id
    )
    db_session.add(patient)
    await db_session.flush()

    assert await public.patient_id_for_user(db_session, user.id) == patient.id
    assert await public.patient_id_for_user(db_session, uuid.uuid4()) is None
