"""POST /notifications against real Postgres: a message staff approved, queued once."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.notifications.models import Notification, NotificationStatus
from app.modules.patients.models import Patient
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def _patient(factory) -> uuid.UUID:
    booking, _ = await confirmed_appointment(factory, RecordingAudit())
    async with factory() as session:
        await session.execute(update(Patient).where(Patient.id == booking.patient_id)
                              .values(email=f"msg-{uuid.uuid4().hex[:8]}@example.test"))
        await session.commit()
    return booking.patient_id


async def test_front_desk_sends_an_approved_message_once(api, token_for, factory):
    """Referenced by tests/cases/nt-010-staff-message-sent-once.yaml."""
    patient_id = await _patient(factory)
    body = {"patient_id": str(patient_id), "subject": "Dr Osei is away next week",
            "body": "Please call us to move your appointment."}
    headers = {**token_for(UserRole.FRONT_DESK), "Idempotency-Key": f"draft-{uuid.uuid4()}"}
    first = await api.post("/notifications", json=body, headers=headers)
    again = await api.post("/notifications", json=body, headers=headers)
    assert first.status_code == 202 and first.json()["status"] == "queued"
    assert again.json() == {"notification_id": None, "status": "duplicate"}
    async with factory() as session:
        rows = (await session.scalars(select(Notification).where(
            Notification.patient_id == patient_id, Notification.kind == "staff_message"))).all()
    [row] = rows
    assert row.status is NotificationStatus.QUEUED
    assert row.subject == "Dr Osei is away next week"
    assert "Please call us to move your appointment." in row.body


async def test_a_provider_without_a_care_relationship_gets_404(api, token_for, factory):
    patient_id = await _patient(factory)
    response = await api.post(
        "/notifications", json={"patient_id": str(patient_id), "subject": "s", "body": "b"},
        headers={**token_for(UserRole.PROVIDER), "Idempotency-Key": "k"})
    assert response.status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}])
async def test_a_message_needs_an_idempotency_key(api, token_for, factory, headers):
    patient_id = await _patient(factory)
    response = await api.post(
        "/notifications", json={"patient_id": str(patient_id), "subject": "s", "body": "b"},
        headers={**token_for(UserRole.FRONT_DESK), **headers})
    assert response.status_code == 422


async def test_a_patient_may_not_send(api, token_for):
    response = await api.post(
        "/notifications", json={"patient_id": str(uuid.uuid4()), "subject": "s", "body": "b"},
        headers={**token_for(UserRole.PATIENT), "Idempotency-Key": "k"})
    assert response.status_code == 403
