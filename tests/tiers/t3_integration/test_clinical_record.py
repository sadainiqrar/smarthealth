"""The clinical record over HTTP, against real Postgres (spec section 9.6).

The lifecycle -- draft, sign, amend -- and above all the access table: who may read which
part of a patient's record. Each test makes its own patient, provider and visit.
"""

from __future__ import annotations

import uuid

import pytest
from fpdf import FPDF
from sqlalchemy import select, update

from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events.models import OutboxEvent
from app.modules.clinical import tasks
from app.modules.clinical.storage import FilesystemDocumentStore
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients.models import Patient
from app.modules.providers.models import Provider
from app.modules.scheduling import service
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, confirmed_appointment

pytestmark = [pytest.mark.integration, pytest.mark.docker]

NOTE = {
    "presenting_complaint": "Breathless on stairs for a month",
    "findings": "BP 152/94; heart sounds normal",
    "clinician_notes": "Consider echo if no better in 6 weeks",
    "patient_summary": "Your blood pressure is high. Start the tablet below and have the blood "
                       "tests; we will review in six weeks.",
    "diagnoses": [{"description": "Essential hypertension", "icd10": "I10"}],
    "prescriptions": [{"medication": "Amlodipine", "dose": "5 mg", "frequency": "once daily",
                       "duration": "6 weeks", "instructions": "Take in the morning"}],
    "orders": [{"test_code": "lipids", "reason": "cardiovascular risk"},
               {"test_code": "hba1c", "urgency": "routine"}],
}


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


async def _login(factory, role: UserRole, *, provider=None, patient=None) -> str:
    async with factory() as session:
        user = User(email=f"{role.value}-{uuid.uuid4().hex[:8]}@example.com",
                    password_hash=hash_password("x"), role=role, is_active=True)
        session.add(user)
        await session.flush()
        if provider is not None:
            await session.execute(update(Provider).where(Provider.id == provider)
                                  .values(user_id=user.id))
        if patient is not None:
            await session.execute(update(Patient).where(Patient.id == patient)
                                  .values(user_id=user.id))
        await session.commit()
        return str(user.id)


@pytest.fixture
async def visit(factory, token_for):
    """A checked-in visit, its provider's and patient's logins, and a stranger provider."""
    b, appointment_id = await confirmed_appointment(factory, RecordingAudit())
    async with factory() as session:
        await service.check_in(session, RecordingAudit(), appointment_id=appointment_id,
                               now=Clock().now(), actor_user_id="desk")
        await session.commit()
    doctor = token_for(UserRole.PROVIDER, await _login(factory, UserRole.PROVIDER,
                                                       provider=b.provider_id))
    patient = token_for(UserRole.PATIENT, await _login(factory, UserRole.PATIENT,
                                                       patient=b.patient_id))
    async with factory() as session:  # a provider who has never seen this patient
        stranger = Provider(first_name="Sam", last_name="Other", specialty="dermatology",
                            license_number=f"LIC-{uuid.uuid4().hex[:10]}")
        session.add(stranger)
        await session.commit()
        stranger_id = stranger.id
    other = token_for(UserRole.PROVIDER, await _login(factory, UserRole.PROVIDER,
                                                      provider=stranger_id))
    yield b, appointment_id, doctor, patient, other
    async with factory() as session:
        await session.execute(Provider.__table__.delete().where(Provider.id == stranger_id))
        await session.commit()


async def _signed(api, appointment_id, doctor):
    assert (await api.put(f"/appointments/{appointment_id}/note", json=NOTE,
                          headers=doctor)).status_code == 200
    signed = await api.post(f"/appointments/{appointment_id}/note/sign", headers=doctor)
    assert signed.status_code == 200, signed.text
    return signed.json()


async def test_a_note_is_drafted_signed_and_amended_as_a_new_version(api, factory, visit):
    """Referenced by tests/cases/cln-001-a-signed-note-is-never-edited.yaml."""
    _, appointment_id, doctor, _, _ = visit
    note = await _signed(api, appointment_id, doctor)
    assert note["version"] == 1 and note["status"] == "signed"
    edit = await api.put(f"/appointments/{appointment_id}/note", json=NOTE, headers=doctor)
    assert edit.status_code == 409, "a signed note is amended, never edited"
    amended = await api.post(f"/appointments/{appointment_id}/note/amend", headers=doctor)
    assert amended.status_code == 201 and amended.json()["version"] == 2
    assert amended.json()["status"] == "draft"
    async with factory() as session:
        events = (await session.scalars(select(OutboxEvent).where(
            OutboxEvent.topic.in_(("clinical.note_signed", "clinical.test_ordered")),
            OutboxEvent.payload["note_id"].astext == note["id"]))).all()
    assert sorted(e.topic for e in events) == [
        "clinical.note_signed", "clinical.test_ordered", "clinical.test_ordered"]
    # D-31: ids and counts only. Nothing of the note's content left Part A.
    flat = str([e.payload for e in events])
    for secret in ("hypertension", "Amlodipine", "lipids", "Breathless"):
        assert secret.lower() not in flat.lower()


async def test_the_access_table(api, visit, token_for):
    """Referenced by tests/cases/cln-002-who-may-read-a-record.yaml (spec 9.6)."""
    b, appointment_id, doctor, patient, other = visit
    await _signed(api, appointment_id, doctor)
    path = f"/patients/{b.patient_id}/record"

    as_doctor = (await api.get(path, headers=doctor)).json()
    assert as_doctor["notes"][0]["clinician_notes"].startswith("Consider echo")
    assert [p["medication"] for p in as_doctor["prescriptions"]] == ["Amlodipine"]

    as_patient = (await api.get("/patients/me/record", headers=patient)).json()
    assert as_patient["notes"][0]["clinician_notes"] is None, "clinician-only notes stay so"
    assert as_patient["notes"][0]["patient_summary"].startswith("Your blood pressure")
    assert {o["test_code"] for o in as_patient["orders"]} == {"lipids", "hba1c"}

    assert (await api.get(path, headers=other)).status_code == 404, "no care relationship"
    assert (await api.get(path, headers=token_for(UserRole.ADMIN))).status_code == 403
    desk = (await api.get(path, headers=token_for(UserRole.FRONT_DESK))).json()
    assert desk["notes"] == [] and desk["prescriptions"] == [] and desk["documents"] == []
    assert all(o["reason"] is None for o in desk["orders"]), "front desk: status only"


async def test_a_patient_sees_no_draft(api, visit):
    _, appointment_id, doctor, patient, _ = visit
    await api.put(f"/appointments/{appointment_id}/note", json=NOTE, headers=doctor)
    response = await api.get(f"/appointments/{appointment_id}/note", headers=patient)
    assert response.status_code == 404


async def test_only_the_appointments_provider_writes_its_note(api, visit):
    _, appointment_id, _, _, other = visit
    response = await api.put(f"/appointments/{appointment_id}/note", json=NOTE, headers=other)
    assert response.status_code == 404


async def test_a_note_without_a_diagnosis_cannot_be_signed(api, visit):
    _, appointment_id, doctor, _, _ = visit
    await api.put(f"/appointments/{appointment_id}/note",
                  json={**NOTE, "diagnoses": []}, headers=doctor)
    response = await api.post(f"/appointments/{appointment_id}/note/sign", headers=doctor)
    assert response.status_code == 422


def _pdf(text: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


async def test_an_uploaded_result_is_extracted_attached_and_reviewed(api, visit, db_settings):
    """Referenced by tests/cases/cln-003-a-patient-uploads-a-result.yaml."""
    b, appointment_id, doctor, patient, _ = visit
    await _signed(api, appointment_id, doctor)
    record = (await api.get("/patients/me/record", headers=patient)).json()
    hba1c = next(o for o in record["orders"] if o["test_code"] == "hba1c")
    assert hba1c["prep_source_id"] == "doc:prep-blood-tests#hba1c"

    upload = await api.post(
        f"/patients/{b.patient_id}/documents",
        params={"filename": "hba1c.pdf", "test_order_id": hba1c["id"]},
        content=_pdf("HbA1c 48 mmol/mol (reference 20-41)"), headers=patient)
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]
    status = await tasks.extract(db_settings, uuid.UUID(document_id),
                                 FilesystemDocumentStore.from_settings(db_settings))
    assert status == "extracted"

    read = (await api.get(f"/patients/{b.patient_id}/documents/{document_id}",
                          headers=doctor)).json()
    assert "48 mmol/mol" in read["extracted_text"] and read["reviewed"] is False
    reviewed = await api.post(f"/test-orders/{hba1c['id']}/review", headers=doctor)
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "reviewed"
    again = (await api.get(f"/patients/{b.patient_id}/documents/{document_id}",
                           headers=patient)).json()
    assert again["reviewed"] is True


async def test_a_file_that_is_not_a_pdf_or_image_is_refused(api, visit):
    b, _, _, patient, _ = visit
    response = await api.post(f"/patients/{b.patient_id}/documents",
                              params={"filename": "evil.pdf"}, content=b"MZ\x90\x00 not a pdf",
                              headers=patient)
    assert response.status_code == 422


async def test_a_patient_cannot_upload_to_someone_elses_record(api, visit, factory, token_for):
    b, _, _, _, _ = visit
    someone = token_for(UserRole.PATIENT, await _login(factory, UserRole.PATIENT))
    response = await api.post(f"/patients/{b.patient_id}/documents",
                              params={"filename": "x.pdf"}, content=_pdf("x"), headers=someone)
    assert response.status_code == 404


