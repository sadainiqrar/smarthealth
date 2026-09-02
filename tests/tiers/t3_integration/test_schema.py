import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db.all_models import ALL_TABLES
from app.modules.identity.models import User, UserRole
from app.modules.patients.models import Patient
from app.modules.providers.models import Clinic, Provider, ProviderSlot, SlotStatus
from app.modules.scheduling.models import Appointment, AppointmentStatus

pytestmark = [pytest.mark.integration, pytest.mark.docker]

BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


async def _clinic_and_provider(session) -> tuple[Clinic, Provider]:
    clinic = Clinic(name=f"Riverside {uuid.uuid4().hex[:8]}", timezone="UTC")
    provider = Provider(
        first_name="Ada", last_name="Lovelace", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}",
    )
    session.add_all([clinic, provider])
    await session.flush()
    return clinic, provider


async def _patient(session) -> Patient:
    patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe"
    )
    session.add(patient)
    await session.flush()
    return patient


async def _slot(session, clinic, provider, *, start=BASE_TIME, minutes=30) -> ProviderSlot:
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
    )
    session.add(slot)
    await session.flush()
    return slot


async def test_every_expected_table_exists_after_migration(db_session):
    present = await db_session.run_sync(
        lambda sync_session: set(inspect(sync_session.connection()).get_table_names())
    )
    assert ALL_TABLES <= present
    assert "alembic_version" in present


async def test_a_patient_can_be_created_without_a_user(db_session):
    """The walk-in case from design spec 3.1, proven against the real schema."""
    patient = await _patient(db_session)
    assert patient.id is not None
    assert patient.user_id is None


async def test_a_patient_can_be_linked_to_a_user(db_session):
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        password_hash="x", role=UserRole.PATIENT,
    )
    db_session.add(user)
    await db_session.flush()
    patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Jo", last_name="Bloggs",
        user_id=user.id,
    )
    db_session.add(patient)
    await db_session.flush()
    assert patient.user_id == user.id


async def test_overlapping_slots_for_one_provider_are_rejected(db_session):
    """The exclusion constraint, not application code, prevents double-bookable
    inventory. A buggy slot generator must fail at write time."""
    clinic, provider = await _clinic_and_provider(db_session)
    await _slot(db_session, clinic, provider)

    db_session.add(
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME + timedelta(minutes=15),
            ends_at=BASE_TIME + timedelta(minutes=45),
        )
    )
    with pytest.raises(IntegrityError, match="no_overlap"):
        await db_session.flush()


async def test_adjacent_slots_are_allowed(db_session):
    """tstzrange is half-open, so 09:00-09:30 and 09:30-10:00 must not collide."""
    clinic, provider = await _clinic_and_provider(db_session)
    await _slot(db_session, clinic, provider)
    await _slot(db_session, clinic, provider, start=BASE_TIME + timedelta(minutes=30))


async def test_a_slot_ending_before_it_starts_is_rejected(db_session):
    clinic, provider = await _clinic_and_provider(db_session)
    db_session.add(
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME, ends_at=BASE_TIME - timedelta(minutes=5),
        )
    )
    with pytest.raises(IntegrityError, match="end_after_start"):
        await db_session.flush()


async def test_two_live_appointments_cannot_hold_one_slot(db_session):
    """The partial unique index makes the invariant a database guarantee, independent
    of whether the booking activity's conditional update is written correctly."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = await _slot(db_session, clinic, provider)
    patient_a = await _patient(db_session)
    patient_b = await _patient(db_session)

    db_session.add(
        Appointment(
            patient_id=patient_a.id, provider_id=provider.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    await db_session.flush()

    db_session.add(
        Appointment(
            patient_id=patient_b.id, provider_id=provider.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    with pytest.raises(IntegrityError, match="ux_appointments_live_slot"):
        await db_session.flush()


async def test_a_cancelled_appointment_frees_the_slot_for_a_new_one(db_session):
    """The index is partial precisely so cancelling releases the slot."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = await _slot(db_session, clinic, provider)
    patient_a = await _patient(db_session)
    patient_b = await _patient(db_session)

    db_session.add(
        Appointment(
            patient_id=patient_a.id, provider_id=provider.id, clinic_id=clinic.id,
            slot_id=slot.id, status=AppointmentStatus.CANCELLED,
        )
    )
    await db_session.flush()

    db_session.add(
        Appointment(
            patient_id=patient_b.id, provider_id=provider.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    await db_session.flush()  # must not raise


async def test_an_appointment_cannot_claim_another_providers_slot(db_session):
    """Every single-column FK is satisfied here; only the composite FK catches it."""
    clinic, alice = await _clinic_and_provider(db_session)
    bob = Provider(
        first_name="Bob", last_name="Stone", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}",
    )
    db_session.add(bob)
    await db_session.flush()
    slot = await _slot(db_session, clinic, alice)
    patient = await _patient(db_session)

    db_session.add(
        Appointment(
            patient_id=patient.id, provider_id=bob.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    with pytest.raises(IntegrityError, match="slot_provider_clinic"):
        await db_session.flush()


async def test_an_appointment_without_a_slot_is_unconstrained(db_session):
    """MATCH SIMPLE leaves the pre-claim state alone — booking starts with no slot."""
    clinic, provider = await _clinic_and_provider(db_session)
    patient = await _patient(db_session)
    db_session.add(
        Appointment(
            patient_id=patient.id, provider_id=provider.id, clinic_id=clinic.id
        )
    )
    await db_session.flush()  # must not raise


async def test_a_confirmed_appointment_must_hold_a_slot(db_session):
    """`confirmed` is only reachable after slot reservation succeeds."""
    clinic, provider = await _clinic_and_provider(db_session)
    patient = await _patient(db_session)
    db_session.add(
        Appointment(
            patient_id=patient.id, provider_id=provider.id, clinic_id=clinic.id,
            status=AppointmentStatus.CONFIRMED,
        )
    )
    with pytest.raises(IntegrityError, match="confirmed_requires_slot"):
        await db_session.flush()


async def test_an_appointment_defaults_to_pending(db_session):
    """Confirmed is reachable only after the workflow succeeds; the database default
    must never be 'confirmed'."""
    clinic, provider = await _clinic_and_provider(db_session)
    patient = await _patient(db_session)
    appointment = Appointment(
        patient_id=patient.id, provider_id=provider.id, clinic_id=clinic.id
    )
    db_session.add(appointment)
    await db_session.flush()
    await db_session.refresh(appointment)
    assert appointment.status is AppointmentStatus.PENDING


async def test_the_atomic_slot_claim_returns_zero_rows_when_already_held(db_session):
    """The Week 2 booking activity depends on this exact behaviour."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = await _slot(db_session, clinic, provider)

    claim = text(
        "UPDATE provider_slots SET status = 'held', version = version + 1 "
        "WHERE id = :slot_id AND status = 'free'"
    )
    first = await db_session.execute(claim, {"slot_id": slot.id})
    assert first.rowcount == 1

    second = await db_session.execute(claim, {"slot_id": slot.id})
    assert second.rowcount == 0

    held = await db_session.execute(
        select(ProviderSlot.status, ProviderSlot.version).where(
            ProviderSlot.id == slot.id
        )
    )
    status, version = held.one()
    assert status is SlotStatus.HELD
    assert version == 1


async def test_a_raw_update_still_bumps_updated_at(db_session):
    """`onupdate=func.now()` does not fire for raw SQL — verified against the live
    database. Week 2's booking activity claims a slot with a raw conditional UPDATE,
    so a trigger, not the ORM default, is what keeps updated_at honest."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = await _slot(db_session, clinic, provider)

    read = text("SELECT updated_at FROM provider_slots WHERE id = :id")
    before = (await db_session.execute(read, {"id": slot.id})).scalar_one()

    await db_session.execute(
        text("UPDATE provider_slots SET status = 'held' WHERE id = :id"),
        {"id": slot.id},
    )
    after = (await db_session.execute(read, {"id": slot.id})).scalar_one()

    assert after > before, "the BEFORE UPDATE trigger did not fire on a raw UPDATE"
