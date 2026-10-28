"""Seed rows for scheduling tests. Committed, because activities and workers read them
from their own sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.patients.models import Patient
from app.modules.providers.models import Clinic, Provider
from app.modules.scheduling.models import Appointment, ProviderSlot


CREATED: list["Bookable"] = []


@dataclass(frozen=True)
class Bookable:
    patient_id: uuid.UUID
    slot_id: uuid.UUID
    provider_id: uuid.UUID
    clinic_id: uuid.UUID


async def seed_bookable(
    session: AsyncSession, *, starts_in: timedelta = timedelta(days=1)
) -> Bookable:
    """Pair with `purge`; the `factory` fixture in test_booking_service does."""
    clinic = Clinic(name=f"Riverside {uuid.uuid4().hex[:8]}", timezone="UTC")
    provider = Provider(
        first_name="Ada", last_name="Lovelace", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}",
    )
    patient = Patient(mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe")
    session.add_all([clinic, provider, patient])
    await session.flush()
    start = datetime.now(UTC).replace(microsecond=0) + starts_in
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=start, ends_at=start + timedelta(minutes=30),
    )
    session.add(slot)
    await session.commit()
    made = Bookable(patient.id, slot.id, provider.id, clinic.id)
    CREATED.append(made)
    return made


async def purge(session: AsyncSession, bookables: list[Bookable]) -> None:
    """Remove what `seed_bookable` committed. The database is shared across the run and
    other tests (the seed's) assert on whole-table counts, so committed rows must not
    outlive the test that made them."""
    for b in bookables:
        await session.execute(delete(Appointment).where(Appointment.patient_id == b.patient_id))
        await session.execute(delete(ProviderSlot).where(ProviderSlot.id == b.slot_id))
        await session.execute(delete(Provider).where(Provider.id == b.provider_id))
        await session.execute(delete(Patient).where(Patient.id == b.patient_id))
        await session.execute(delete(Clinic).where(Clinic.id == b.clinic_id))
    await session.commit()

