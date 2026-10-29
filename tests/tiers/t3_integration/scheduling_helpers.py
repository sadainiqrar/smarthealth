"""Seed rows for scheduling tests. Committed, because activities and workers read them
from their own sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.patients.models import Patient
from app.modules.providers.models import Clinic, Provider
from app.modules.scheduling import service
from app.modules.scheduling.models import Appointment, ProviderSlot


CREATED: list["Bookable"] = []


@dataclass(frozen=True)
class Bookable:
    patient_id: uuid.UUID
    slot_id: uuid.UUID
    provider_id: uuid.UUID
    clinic_id: uuid.UUID


async def seed_bookable(
    session: AsyncSession, *, starts_in: timedelta = timedelta(days=1),
    duration: timedelta = timedelta(minutes=30), starts_at: datetime | None = None,
    patient_id: uuid.UUID | None = None,
) -> Bookable:
    """Pair with `purge`; the `factory` fixture in test_booking_service does.

    `starts_at` pins the slot to an exact instant (two seeds that must overlap cannot
    rely on `now` agreeing between them); `patient_id` reuses an existing patient
    instead of making one, for a second provider's slot booked by the same patient."""
    clinic = Clinic(name=f"Riverside {uuid.uuid4().hex[:8]}", timezone="UTC")
    provider = Provider(
        first_name="Ada", last_name="Lovelace", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}",
    )
    session.add_all([clinic, provider])
    if patient_id is None:
        patient = Patient(mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe")
        session.add(patient)
        await session.flush()
        patient_id = patient.id
    await session.flush()
    start = starts_at or datetime.now(UTC).replace(microsecond=0) + starts_in
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=start, ends_at=start + duration,
    )
    session.add(slot)
    await session.commit()
    made = Bookable(patient_id, slot.id, provider.id, clinic.id)
    CREATED.append(made)
    return made


class RecordingAudit:
    """An `AuditLog` stand-in that keeps what it was asked to record."""

    def __init__(self) -> None:
        self.events: list = []

    async def record(self, event) -> None:
        self.events.append(event)


async def confirmed_appointment(factory, audit=None) -> tuple[Bookable, uuid.UUID]:
    """Seed a bookable slot and take a booking all the way to `confirmed` through the
    service functions, each in its own committed transaction -- the same shape the
    booking activities give it. Pair with `purge` like `seed_bookable`."""
    audit = audit or RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    appointment_id = uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=appointment_id, patient_id=b.patient_id,
            slot_id=b.slot_id, reason=None, actor_user_id=None,
        )
        await session.commit()
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=appointment_id, slot_id=b.slot_id,
            now=Clock().now(), actor_user_id=None,
        )
        await session.commit()
    async with factory() as session:
        await service.confirm_booking(
            session, audit, appointment_id=appointment_id, now=Clock().now(),
            actor_user_id=None,
        )
        await session.commit()
    return b, appointment_id



async def confirmed_replacement(
    factory, audit, b: Bookable, old_id: uuid.UUID, *,
    starts_in: timedelta = timedelta(days=2),
) -> tuple[uuid.UUID, uuid.UUID]:
    """`(new_appointment_id, new_slot_id)`: what a reschedule's child booking leaves once
    it confirms -- a new appointment for `b`'s patient on a fresh slot, linked back to
    `old_id`, taken to `confirmed` step by step as `confirmed_appointment` does."""
    async with factory() as session:
        slot_id = await add_slot(session, b, starts_in=starts_in)
    new_id = uuid.uuid4()
    async with factory() as session:
        await service.create_pending(
            session, audit, appointment_id=new_id, patient_id=b.patient_id,
            slot_id=slot_id, reason=None, actor_user_id=None, rescheduled_from_id=old_id,
        )
        await session.commit()
    async with factory() as session:
        await service.claim_slot(
            session, audit, appointment_id=new_id, slot_id=slot_id, now=Clock().now(),
            actor_user_id=None,
        )
        await session.commit()
    async with factory() as session:
        await service.confirm_booking(
            session, audit, appointment_id=new_id, now=Clock().now(), actor_user_id=None,
        )
        await session.commit()
    return new_id, slot_id

async def add_slot(
    session: AsyncSession, b: Bookable, *, starts_in: timedelta = timedelta(days=2)
) -> uuid.UUID:
    """Another slot for the same provider and clinic. `purge` removes it with the rest.
    The default start keeps it clear of `seed_bookable`'s, which the GiST exclusion
    constraint would otherwise reject as an overlap."""
    start = datetime.now(UTC).replace(microsecond=0) + starts_in
    slot = ProviderSlot(
        provider_id=b.provider_id, clinic_id=b.clinic_id,
        starts_at=start, ends_at=start + timedelta(minutes=30),
    )
    session.add(slot)
    await session.commit()
    return slot.id


async def purge(session: AsyncSession, bookables: list[Bookable]) -> None:
    """Remove what `seed_bookable` and `add_slot` committed. The database is shared across
    the run and other tests (the seed's) assert on whole-table counts, so committed rows
    must not outlive the test that made them."""
    for b in bookables:
        await session.execute(delete(Appointment).where(Appointment.patient_id == b.patient_id))
        await session.execute(
            delete(ProviderSlot).where(ProviderSlot.provider_id == b.provider_id)
        )
        await session.execute(delete(Provider).where(Provider.id == b.provider_id))
        await session.execute(delete(Patient).where(Patient.id == b.patient_id))
        await session.execute(delete(Clinic).where(Clinic.id == b.clinic_id))
    await session.commit()

