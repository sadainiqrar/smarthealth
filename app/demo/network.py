"""The demo clinic network: who works where, which patients exist, which logins.

Natural keys make every write idempotent, as in `app/seed.py`: clinic name, provider licence
(`DEMO-LIC-…`), patient MRN (`DEMO-…`), user email. The `DEMO-` prefixes are also what the
safety guard in `app.demo` recognises as demo data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.modules.identity.models import UserRole

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
