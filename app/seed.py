"""Demonstration data for a running system.

`python -m app.cli seed` fills an empty database with enough of a clinic network to
exercise the Week 1 endpoints by hand: clinics in two timezones, departments,
providers with specialties, a login per role, a mix of patients with and without user
accounts, and a bounded window of bookable slots.

**What this deliberately does not seed, and why.** No appointments, no visits, no
waitlist entries. Those are Week 2's, and they are *workflow outputs* rather than
rows: an appointment starts `pending` and reaches `confirmed` only after slot
reservation, the billing pre-check and notification scheduling have all succeeded.
Hand-writing a `confirmed` row would manufacture state the workflow never produced,
and Week 2's tests would then be validating against fiction. A visit exists because
someone checked in. Seeding either would be the fastest way to make this data a
liability.

**Why a CLI command and not a migration.** A migration runs in every environment,
including each isolated database the test harness creates per run, and it becomes
immutable history that later migrations must keep working around. Seed data is
neither of those things: it is optional, environment-specific, and disposable.

**Idempotent.** Every entity is looked up by its natural key -- clinic name, provider
licence number, patient MRN, user email -- and skipped if present, so running twice
changes nothing. `--clear` removes exactly the rows these natural keys identify and
nothing else; if Week 2 has booked a seeded slot, the delete fails loudly on the
appointment's RESTRICT foreign key rather than cascading through real data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients.models import Patient
from app.modules.providers.models import (
    Clinic,
    Department,
    Provider,
    ProviderSlot,
    SlotStatus,
    provider_departments,
)

# --- The fixed shape of the demonstration network -------------------------------
#
# Natural keys, not ids: these literals are what makes the seeder idempotent and what
# `--clear` matches on. Changing one orphans the row it used to identify.

CLINICS: tuple[tuple[str, str, str], ...] = (
    ("MediNova Downtown", "America/New_York", "410 Cedar Street, Riverbend"),
    ("MediNova Westside", "America/Los_Angeles", "88 Harbour Road, Westport"),
)

#: (clinic name, department name)
DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("MediNova Downtown", "Cardiology"),
    ("MediNova Downtown", "General Practice"),
    ("MediNova Downtown", "Radiology"),
    ("MediNova Westside", "General Practice"),
    ("MediNova Westside", "Dermatology"),
)

#: (licence, first, last, specialty, clinic, department)
PROVIDERS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("LIC-100001", "Amara", "Osei", "Cardiology", "MediNova Downtown", "Cardiology"),
    ("LIC-100002", "Ravi", "Chandrasekhar", "General Practice",
     "MediNova Downtown", "General Practice"),
    ("LIC-100003", "Helena", "Vasquez", "Radiology", "MediNova Downtown", "Radiology"),
    ("LIC-100004", "Tomas", "Lindqvist", "Dermatology", "MediNova Westside", "Dermatology"),
    ("LIC-100005", "Grace", "Abara", "General Practice",
     "MediNova Westside", "General Practice"),
)

#: (email, role). One login per role so every authorisation path is reachable by hand.
#: The provider account is linked to a provider record below; the patient account to a
#: patient record, which is what makes "a user is not a person" visible in the data.
USERS: tuple[tuple[str, UserRole], ...] = (
    ("admin@medinova.example", UserRole.ADMIN),
    ("frontdesk@medinova.example", UserRole.FRONT_DESK),
    ("a.osei@medinova.example", UserRole.PROVIDER),
    ("j.okonkwo@example.com", UserRole.PATIENT),
)

#: The provider and patient whose records carry a `user_id`. Everyone else has none --
#: most patients are walk-ins registered by front-desk staff and never log in, which is
#: the whole reason `patients.user_id` is nullable.
LINKED_PROVIDER_LICENCE = "LIC-100001"
LINKED_PATIENT_MRN = "MRN-000001"

_FIRST_NAMES = (
    "Jelani", "Marta", "Sunil", "Ines", "Kofi", "Larisa", "Yusuf", "Petra",
    "Nadia", "Emeka", "Rosa", "Hakim", "Dagny", "Tariq", "Maeve", "Bo",
)
_LAST_NAMES = (
    "Okonkwo", "Silva", "Reddy", "Novak", "Mensah", "Petrova", "Demir", "Kowalski",
    "Haddad", "Eze", "Marchetti", "Farouk", "Sorensen", "Rahman", "Brennan", "Zhang",
)

# --- Slot generation -------------------------------------------------------------

#: Slots are generated into a bounded future window. Week 2 will grow a real
#: availability generator; if it produces slots for these providers in this range, the
#: GiST exclusion constraint rejects the overlap **loudly**, which is the failure we
#: want. Silent duplicate inventory is the one that reaches a patient.
SLOT_WINDOW_DAYS = 14
SLOT_DURATION = timedelta(minutes=30)
#: 09:00-12:00, so six slots per provider per weekday.
SLOT_DAY_START = time(9, 0)
SLOT_DAY_END = time(12, 0)


@dataclass(frozen=True)
class SeedSummary:
    """What a run actually changed, so the command can report rather than claim."""

    clinics: int = 0
    departments: int = 0
    providers: int = 0
    patients: int = 0
    users: int = 0
    slots: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"  clinics      {self.clinics}",
            f"  departments  {self.departments}",
            f"  providers    {self.providers}",
            f"  patients     {self.patients}",
            f"  users        {self.users}",
            f"  slots        {self.slots}",
        ]


def _patient_identity(index: int) -> tuple[str, str, str]:
    """Deterministic name and MRN for patient `index`, so reruns match existing rows."""
    first = _FIRST_NAMES[index % len(_FIRST_NAMES)]
    last = _LAST_NAMES[(index // len(_FIRST_NAMES) + index) % len(_LAST_NAMES)]
    return f"MRN-{index + 1:06d}", first, last


def _slot_starts(now: datetime) -> list[datetime]:
    """Every slot start in the window, weekdays only.

    Anchored to tomorrow rather than now, so a seeded slot is never already in the
    past -- which the booking activity will reject in Week 2, and which would make the
    seeded data useless for demonstrating a booking.
    """
    first_day = (now + timedelta(days=1)).date()
    starts: list[datetime] = []
    for offset in range(SLOT_WINDOW_DAYS):
        day = first_day + timedelta(days=offset)
        if day.weekday() >= 5:  # Saturday, Sunday
            continue
        cursor = datetime.combine(day, SLOT_DAY_START, tzinfo=now.tzinfo)
        day_end = datetime.combine(day, SLOT_DAY_END, tzinfo=now.tzinfo)
        while cursor + SLOT_DURATION <= day_end:
            starts.append(cursor)
            cursor += SLOT_DURATION
    return starts


async def seed(
    session: AsyncSession,
    *,
    password: str,
    patient_count: int,
    clock: Clock | None = None,
) -> SeedSummary:
    """Insert the demonstration network. Safe to run repeatedly.

    The caller owns the transaction, matching every other service in this codebase:
    this function flushes so that ids are available for the rows that reference them,
    and never commits.
    """
    now = (clock or Clock()).now()
    counts = dict.fromkeys(("clinics", "departments", "providers", "patients", "users", "slots"), 0)

    # --- clinics ---
    clinics: dict[str, Clinic] = {}
    for name, timezone, address in CLINICS:
        clinic = await session.scalar(select(Clinic).where(Clinic.name == name))
        if clinic is None:
            clinic = Clinic(name=name, timezone=timezone, address=address)
            session.add(clinic)
            counts["clinics"] += 1
        clinics[name] = clinic
    await session.flush()

    # --- departments ---
    departments: dict[tuple[str, str], Department] = {}
    for clinic_name, dept_name in DEPARTMENTS:
        clinic = clinics[clinic_name]
        department = await session.scalar(
            select(Department).where(
                Department.clinic_id == clinic.id, Department.name == dept_name
            )
        )
        if department is None:
            department = Department(clinic_id=clinic.id, name=dept_name)
            session.add(department)
            counts["departments"] += 1
        departments[(clinic_name, dept_name)] = department
    await session.flush()

    # --- providers, and their department membership ---
    providers: dict[str, Provider] = {}
    for licence, first, last, specialty, _clinic, _dept in PROVIDERS:
        provider = await session.scalar(
            select(Provider).where(Provider.license_number == licence)
        )
        if provider is None:
            provider = Provider(
                license_number=licence,
                first_name=first,
                last_name=last,
                specialty=specialty,
            )
            session.add(provider)
            counts["providers"] += 1
        providers[licence] = provider
    await session.flush()

    for licence, _first, _last, _specialty, clinic_name, dept_name in PROVIDERS:
        provider = providers[licence]
        department = departments[(clinic_name, dept_name)]
        already = await session.scalar(
            select(provider_departments.c.provider_id).where(
                provider_departments.c.provider_id == provider.id,
                provider_departments.c.department_id == department.id,
            )
        )
        if already is None:
            await session.execute(
                provider_departments.insert().values(
                    provider_id=provider.id, department_id=department.id
                )
            )

    # --- patients ---
    patients: dict[str, Patient] = {}
    for index in range(patient_count):
        mrn, first, last = _patient_identity(index)
        patient = await session.scalar(select(Patient).where(Patient.mrn == mrn))
        if patient is None:
            patient = Patient(
                mrn=mrn,
                first_name=first,
                last_name=last,
                date_of_birth=None,
                phone=f"+1-555-{index:04d}",
                email=None,
            )
            session.add(patient)
            counts["patients"] += 1
        patients[mrn] = patient
    await session.flush()

    # --- logins, one per role ---
    #
    # Hashed with the same argon2 configuration the login endpoint verifies against,
    # so a seeded account genuinely authenticates. The password is supplied by the
    # caller and never defaulted: a well-known credential written into a database by
    # a tool that did not ask is how a demo database becomes a production incident.
    for email, role in USERS:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
            session.add(user)
            counts["users"] += 1
        await session.flush()

        # A user is not a person: only these two are linked to a domain record, and
        # every other patient below has `user_id` NULL -- the walk-in case that made
        # the column nullable in the first place.
        if role is UserRole.PROVIDER:
            linked = providers.get(LINKED_PROVIDER_LICENCE)
            if linked is not None and linked.user_id is None:
                linked.user_id = user.id
        elif role is UserRole.PATIENT:
            linked_patient = patients.get(LINKED_PATIENT_MRN)
            if linked_patient is not None and linked_patient.user_id is None:
                linked_patient.user_id = user.id
    await session.flush()

    # --- bookable slots ---
    #
    # One pass per provider over the window. Existing starts are read once and
    # subtracted rather than queried per slot, so a rerun costs one SELECT per
    # provider instead of several hundred.
    starts = _slot_starts(now)
    for licence, _first, _last, _specialty, clinic_name, dept_name in PROVIDERS:
        provider = providers[licence]
        clinic = clinics[clinic_name]
        department = departments[(clinic_name, dept_name)]

        existing = set(
            (
                await session.scalars(
                    select(ProviderSlot.starts_at).where(
                        ProviderSlot.provider_id == provider.id
                    )
                )
            ).all()
        )
        for start in starts:
            if start in existing:
                continue
            session.add(
                ProviderSlot(
                    provider_id=provider.id,
                    clinic_id=clinic.id,
                    department_id=department.id,
                    starts_at=start,
                    ends_at=start + SLOT_DURATION,
                    status=SlotStatus.FREE,
                )
            )
            counts["slots"] += 1
    await session.flush()

    return SeedSummary(**counts)


async def clear(session: AsyncSession, *, patient_count: int) -> SeedSummary:
    """Remove exactly the rows the seeder creates, identified by natural key.

    Deletion runs child-first because the schema uses RESTRICT where a cascade would
    be dangerous. If Week 2 has booked one of these slots, the slot delete fails on
    the appointment's foreign key -- correctly. That is a signal to look at what is
    referencing the data, not something to force past.

    Patients and providers are deliberately *not* deleted when an appointment
    references them, for the same reason: RESTRICT will say so.
    """
    counts = dict.fromkeys(("clinics", "departments", "providers", "patients", "users", "slots"), 0)

    clinic_names = [name for name, _tz, _addr in CLINICS]
    clinic_ids = list(
        (await session.scalars(select(Clinic.id).where(Clinic.name.in_(clinic_names)))).all()
    )
    provider_ids = list(
        (
            await session.scalars(
                select(Provider.id).where(
                    Provider.license_number.in_([p[0] for p in PROVIDERS])
                )
            )
        ).all()
    )

    if provider_ids:
        result = await session.execute(
            delete(ProviderSlot).where(ProviderSlot.provider_id.in_(provider_ids))
        )
        counts["slots"] = result.rowcount or 0
        await session.execute(
            provider_departments.delete().where(
                provider_departments.c.provider_id.in_(provider_ids)
            )
        )

    if provider_ids:
        result = await session.execute(
            delete(Provider).where(Provider.id.in_(provider_ids))
        )
        counts["providers"] = result.rowcount or 0

    # Exact MRNs, never a LIKE pattern. `MRN-______` would also match a real patient
    # who happens to use that format, and a clear command that deletes rows it did not
    # create is worse than one that leaves some behind.
    result = await session.execute(
        delete(Patient).where(Patient.mrn.in_(seeded_patient_mrns(patient_count)))
    )
    counts["patients"] = result.rowcount or 0

    if clinic_ids:
        result = await session.execute(
            delete(Department).where(Department.clinic_id.in_(clinic_ids))
        )
        counts["departments"] = result.rowcount or 0
        result = await session.execute(delete(Clinic).where(Clinic.id.in_(clinic_ids)))
        counts["clinics"] = result.rowcount or 0

    result = await session.execute(
        delete(User).where(User.email.in_([email for email, _role in USERS]))
    )
    counts["users"] = result.rowcount or 0

    return SeedSummary(**counts)


def seeded_patient_mrns(count: int) -> list[str]:
    """The MRNs `seed` would use for `count` patients. For tests and for `--clear`."""
    return [_patient_identity(index)[0] for index in range(count)]


__all__ = ["SeedSummary", "clear", "seed", "seeded_patient_mrns"]
