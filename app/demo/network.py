"""The demo clinic network: who works where, which patients exist, which logins.

Natural keys make every write idempotent, as in `app/seed.py`: clinic name, provider licence
(`DEMO-LIC-…`), patient MRN (`DEMO-…`), user email. The `DEMO-` prefixes are also what the
safety guard in `app.demo` recognises as demo data.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients.models import Patient
from app.modules.providers import service as providers_service
from app.modules.providers.models import Clinic, Department, Provider, provider_departments
from app.modules.scheduling.models import ProviderSlot, SlotStatus

DEFAULT_TIMEZONE = "Australia/Sydney"
MRN_PREFIX = "DEMO-"
LICENCE_PREFIX = "DEMO-LIC-"
PATIENT_COUNT = 800
SESSION_START = time(9, 0)
SLOTS_PER_SESSION = 6
SLOT_DURATION = timedelta(minutes=30)
UPCOMING_DAYS = 14

#: (name, address)
CLINICS: tuple[tuple[str, str], ...] = (
    ("MediNova Harbourside", "12 Wharf Street, Harbourside"),
    ("MediNova Northgate", "240 Ridge Road, Northgate"),
    ("MediNova Parkview", "5 Garden Avenue, Parkview"),
)


@dataclass(frozen=True)
class DemoProvider:
    licence: str
    first: str
    last: str
    specialty: str
    clinic: str
    weekdays: tuple[int, int]  # Monday = 0

    @property
    def email(self) -> str:
        return f"{self.first}.{self.last}@medinova.example".lower()


PROVIDERS: tuple[DemoProvider, ...] = (
    DemoProvider("DEMO-LIC-001", "Amara", "Osei", "Cardiology", "MediNova Harbourside", (0, 2)),
    DemoProvider("DEMO-LIC-002", "Lena", "Fischer", "Dermatology", "MediNova Parkview", (1, 3)),
    DemoProvider("DEMO-LIC-003", "Kenji", "Watanabe", "Endocrinology", "MediNova Northgate",
                 (0, 3)),
    DemoProvider("DEMO-LIC-004", "Priya", "Raman", "Gastroenterology", "MediNova Harbourside",
                 (1, 4)),
    DemoProvider("DEMO-LIC-005", "Ravi", "Chandrasekhar", "General Practice",
                 "MediNova Harbourside", (0, 3)),
    DemoProvider("DEMO-LIC-006", "Grace", "Abara", "General Practice", "MediNova Northgate",
                 (1, 4)),
    DemoProvider("DEMO-LIC-007", "Mateo", "Alvarez", "Neurology", "MediNova Parkview", (0, 2)),
    DemoProvider("DEMO-LIC-008", "Sofia", "Lindgren", "Orthopaedics", "MediNova Northgate",
                 (2, 4)),
    DemoProvider("DEMO-LIC-009", "Noor", "Haddad", "Paediatrics", "MediNova Harbourside", (1, 3)),
    DemoProvider("DEMO-LIC-010", "Daniel", "Mensah", "Paediatrics", "MediNova Parkview", (0, 4)),
    DemoProvider("DEMO-LIC-011", "Hannah", "Brooks", "Physiotherapy", "MediNova Northgate",
                 (0, 2)),
    DemoProvider("DEMO-LIC-012", "Oliver", "Grant", "Physiotherapy", "MediNova Parkview", (1, 3)),
    DemoProvider("DEMO-LIC-013", "Mei", "Lin", "Ear, Nose and Throat (ENT)",
                 "MediNova Harbourside", (2, 4)),
    DemoProvider("DEMO-LIC-014", "Samuel", "Okafor", "Cardiology", "MediNova Northgate", (1, 4)),
    DemoProvider("DEMO-LIC-015", "Elena", "Petrova", "Orthopaedics", "MediNova Harbourside",
                 (0, 3)),
)
PROVIDERS_BY_LICENCE: dict[str, DemoProvider] = {p.licence: p for p in PROVIDERS}

#: Staff logins with no domain record.
STAFF_LOGINS: tuple[tuple[str, UserRole], ...] = (
    ("demo.admin@medinova.example", UserRole.ADMIN),
    ("demo.desk1@medinova.example", UserRole.FRONT_DESK),
    ("demo.desk2@medinova.example", UserRole.FRONT_DESK),
)

#: Patients with a login (their record email is their login). All adults.
LINKED_PATIENT_INDEXES: tuple[int, ...] = (1, 2, 3, 4, 5)

_FIRST = (
    "Ava", "Liam", "Mia", "Noah", "Zara", "Ethan", "Chloe", "Arjun", "Isla", "Mateo", "Leah",
    "Omar", "Ruby", "Hugo", "Sienna", "Kai", "Amelia", "Felix", "Maya", "Jonah", "Elif",
    "Tariq", "Nina", "Theo",
)
_LAST = (
    "Nguyen", "Smith", "Patel", "Kowalski", "Rossi", "Haddad", "Okafor", "Silva", "Novak",
    "Murphy", "Tanaka", "Ahmed", "Larsen", "Costa", "Bennett", "Moreau", "Kaur", "Fraser",
    "Ibrahim", "Walsh", "Dubois", "Ryan", "Santos", "Khan",
)


@dataclass(frozen=True)
class DemoPatient:
    index: int
    mrn: str
    first: str
    last: str
    date_of_birth: date
    phone: str
    email: str


def is_child(index: int) -> bool:
    """About one patient in seven is a child; Paediatrics sees only them."""
    return index % 7 == 0


def patient_identity(index: int) -> DemoPatient:
    """Deterministic for `index`, independent of today, so re-runs match existing rows."""
    rng = random.Random(f"smarthealth-demo-patient:{index}")
    first = _FIRST[index % len(_FIRST)]
    last = _LAST[(index // len(_FIRST) + index) % len(_LAST)]
    if is_child(index):
        born = date(2012, 1, 1) + timedelta(days=rng.randrange(12 * 365))
    else:
        born = date(1941, 1, 1) + timedelta(days=rng.randrange(65 * 365))
    return DemoPatient(
        index=index, mrn=f"{MRN_PREFIX}{index + 1:06d}", first=first, last=last,
        date_of_birth=born, phone=f"+61 4{index:08d}",
        email=f"{first}.{last}{index + 1}@example.com".lower(),
    )


def working(provider: DemoProvider, day: date) -> bool:
    return day.weekday() in provider.weekdays


def session_starts(day: date, tz: ZoneInfo) -> list[datetime]:
    """The six slot starts of a morning session on `day`, in UTC."""
    first = datetime.combine(day, SESSION_START, tzinfo=tz)
    return [(first + i * SLOT_DURATION).astimezone(UTC) for i in range(SLOTS_PER_SESSION)]


@dataclass
class Network:
    """Ids of the demo network's rows -- plain values, safe to use after the session ends."""

    timezone: str
    clinic_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    department_ids: dict[tuple[str, str], uuid.UUID] = field(default_factory=dict)
    provider_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    provider_user_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    patient_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


async def build_network(
    session: AsyncSession, *, password: str, timezone: str = DEFAULT_TIMEZONE
) -> tuple[Network, dict[str, int]]:
    """Create whatever of the network is missing; return its ids and what was created.

    Flushes, never commits (the caller owns the transaction, as in `app/seed.py`). An existing
    clinic keeps its timezone: `demo-refresh` must not move clinics that `demo-seed` placed.
    """
    created = dict.fromkeys(("clinics", "departments", "providers", "patients", "users"), 0)
    clinics: dict[str, Clinic] = {}
    for name, address in CLINICS:
        clinic = await session.scalar(select(Clinic).where(Clinic.name == name))
        if clinic is None:
            clinic = Clinic(name=name, timezone=timezone, address=address)
            session.add(clinic)
            created["clinics"] += 1
        clinics[name] = clinic
    await session.flush()
    net = Network(timezone=clinics[CLINICS[0][0]].timezone)
    net.clinic_ids = {name: c.id for name, c in clinics.items()}

    for p in PROVIDERS:
        key = (p.clinic, p.specialty)
        if key in net.department_ids:
            continue
        department = await session.scalar(select(Department).where(
            Department.clinic_id == clinics[p.clinic].id, Department.name == p.specialty))
        if department is None:
            department = Department(clinic_id=clinics[p.clinic].id, name=p.specialty)
            session.add(department)
            await session.flush()
            created["departments"] += 1
        net.department_ids[key] = department.id

    providers: dict[str, Provider] = {}
    for p in PROVIDERS:
        provider = await session.scalar(
            select(Provider).where(Provider.license_number == p.licence))
        if provider is None:
            provider = Provider(license_number=p.licence, first_name=p.first, last_name=p.last,
                                specialty=p.specialty)
            session.add(provider)
            created["providers"] += 1
        providers[p.licence] = provider
    await session.flush()
    for p in PROVIDERS:
        provider_id = providers[p.licence].id
        department_id = net.department_ids[(p.clinic, p.specialty)]
        member = await session.scalar(select(provider_departments.c.provider_id).where(
            provider_departments.c.provider_id == provider_id,
            provider_departments.c.department_id == department_id))
        if member is None:
            await session.execute(provider_departments.insert().values(
                provider_id=provider_id, department_id=department_id))
        net.provider_ids[p.licence] = provider_id

    wanted = [patient_identity(i).mrn for i in range(PATIENT_COUNT)]
    existing = {row.mrn: row for row in (await session.scalars(
        select(Patient).where(Patient.mrn.in_(wanted)))).all()}
    patients: list[Patient] = []
    for index in range(PATIENT_COUNT):
        who = patient_identity(index)
        patient = existing.get(who.mrn)
        if patient is None:
            patient = Patient(mrn=who.mrn, first_name=who.first, last_name=who.last,
                              date_of_birth=who.date_of_birth, phone=who.phone, email=who.email)
            session.add(patient)
            created["patients"] += 1
        patients.append(patient)
    await session.flush()
    net.patient_ids = [p.id for p in patients]

    logins: list[tuple[str, UserRole]] = list(STAFF_LOGINS)
    logins += [(p.email, UserRole.PROVIDER) for p in PROVIDERS]
    logins += [(patient_identity(i).email, UserRole.PATIENT) for i in LINKED_PATIENT_INDEXES]
    users: dict[str, User] = {}
    for email, role in logins:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, password_hash=hash_password(password), role=role,
                        is_active=True)
            session.add(user)
            created["users"] += 1
        users[email] = user
    await session.flush()
    for p in PROVIDERS:
        provider = providers[p.licence]
        if provider.user_id is None:
            provider.user_id = users[p.email].id
        net.provider_user_ids[p.licence] = provider.user_id
    for index in LINKED_PATIENT_INDEXES:
        if patients[index].user_id is None:
            patients[index].user_id = users[patient_identity(index).email].id
    await session.flush()

    # Announce clinics, departments and providers, so Part B builds its own copies (the
    # knowledge service indexes each provider), exactly as `seed` does.
    await providers_service.republish_all(session)
    return net, created


async def ensure_slots(
    session: AsyncSession, net: Network, *, first_day: date, last_day: date
) -> int:
    """Insert `free` slots for every session between the two days (inclusive) that has none
    yet. Inserts only -- never an update -- so it stays clear of the slot-write guard.

    Call it only for future days (from tomorrow): it does not check `starts_at > now`, and a
    window overlapping slots of other lengths would hit the no-overlap constraint."""
    made = 0
    tz = net.tz
    days = [first_day + timedelta(days=n) for n in range((last_day - first_day).days + 1)]
    for p in PROVIDERS:
        provider_id = net.provider_ids[p.licence]
        starts = [s for day in days if working(p, day) for s in session_starts(day, tz)]
        if not starts:
            continue
        existing = set((await session.scalars(select(ProviderSlot.starts_at).where(
            ProviderSlot.provider_id == provider_id,
            ProviderSlot.starts_at >= min(starts), ProviderSlot.starts_at <= max(starts)))).all())
        for start in starts:
            if start in existing:
                continue
            session.add(ProviderSlot(
                provider_id=provider_id, clinic_id=net.clinic_ids[p.clinic],
                department_id=net.department_ids[(p.clinic, p.specialty)],
                starts_at=start, ends_at=start + SLOT_DURATION, status=SlotStatus.FREE))
            made += 1
    await session.flush()
    return made
