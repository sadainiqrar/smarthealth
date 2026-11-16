# Demo Seed Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two CLI commands, `demo-seed` and `demo-refresh`, that give a deployed SmartHealth a lived-in clinic network: six months of imported history (marked `origin = 'legacy_import'`) plus live activity booked through the real API.

**Architecture:** A new package `app/demo/` beside `app/seed.py` (outside `app/modules`, so it may read every module's models). Generation is **pure and deterministic** (`plan_day` returns plain dataclasses, seeded by the date) and kept apart from **writing** (`import_day` inserts rows, one committed transaction per day, so re-runs skip finished days). Live activity is an `httpx` client that logs in as demo users and books, cancels, reschedules and joins the waitlist through Part A's HTTP API.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, httpx, pytest (markers `unit`, `integration`, `docker`), the compose test stack for T3.

**Spec:** `docs/superpowers/specs/2026-11-16-demo-seed-data-design.md`. Branch: `feat/demo-seed-data`.

---

## Ground rules the engineer must know

- **Never assign `.status` on any object in `app/demo/`.** `tests/meta/test_slot_writes.py` flags an attribute assignment to `status` in any file that mentions `ProviderSlot`/`SlotStatus`. Pass `status=` as a constructor keyword only (inserts are allowed).
- **Every past slot is inserted, never updated.** Upcoming slots are inserted `free`; history slots are inserted with their final status.
- **Analytics days are UTC days** (`app/modules/scheduling/public.py:daily_counts` groups by `timezone('UTC', …)::date`). Clinic hours are local (`Australia/Sydney` by default): 09:00 Sydney is 22:00 or 23:00 UTC the *previous* day. Always derive analytics days with `.astimezone(UTC).date()`.
- **Integration tests share one database per run** (`db_settings` is session-scoped) and other tests assert on whole tables, so every demo test purges what it created (Task 6 adds `purge_demo`).
- **Two Stop hooks guard this repo:** a changed `app/cli.py` needs a validated case YAML (Task 9), and changed application code needs a doc change (Task 10).
- Run fast tests: `python -m pytest -m "not docker" -q`. Integration: `python -m pytest -m docker -q <path>` (needs the compose test stack — use the `test-stack` skill to bring it up).
- Lint: `python -m ruff check app tests services` (line length 100).

## File structure

| File | Responsibility |
| --- | --- |
| `app/demo/content.py` | Plain data: specialties, clinical cases per specialty, medications, test codes, cancellation reasons, result texts |
| `app/demo/network.py` | Network constants and pure identity helpers; `build_network` and `ensure_slots` (idempotent writes); the `Network` id map |
| `app/demo/history.py` | `plan_day` (pure, deterministic) and `import_day` / `day_imported` / `last_imported_day` (writes); `result_pdf` |
| `app/demo/live.py` | `LiveClient` (HTTP) and `run_live` (books, cancels, reschedules, waitlists) |
| `app/demo/__init__.py` | `demo_seed`, `demo_refresh`, `DemoRefused`, `DemoSummary` — orchestration and the safety guard |
| `app/cli.py` | `demo-seed` and `demo-refresh` subcommands |
| `tests/tiers/t0_unit/test_demo_content.py` | Content covers every specialty; codes exist in the catalogue |
| `tests/tiers/t0_unit/test_demo_network.py` | Identities and schedules are deterministic and well-formed |
| `tests/tiers/t0_unit/test_demo_history.py` | `plan_day` determinism, rates, overlaps, children, UTC; `result_pdf` reads back |
| `tests/tiers/t0_unit/test_demo_cli.py` | The CLI refuses without `--allow-demo-data` |
| `tests/tiers/t3_integration/demo_helpers.py` | `purge_demo` for tests |
| `tests/tiers/t3_integration/test_demo_data.py` | Constraints hold, idempotent, zero analytics drift, guard, live phase |
| `tests/cases/ops-005-demo-history-is-consistent-and-idempotent.yaml` | Case for the core-path Stop hook |

---

### Task 1: Clinical content

**Files:**
- Create: `app/demo/__init__.py` (empty for now — one-line docstring)
- Create: `app/demo/content.py`
- Test: `tests/tiers/t0_unit/test_demo_content.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tiers/t0_unit/test_demo_content.py
"""The demo's clinical content: every documented specialty has cases, and every test code
exists in the real catalogue -- an imported order with an unknown code would break the
patient record's preparation guidance."""

from __future__ import annotations

import pytest

from app.demo.content import CANCELLATION_REASONS, CONTENT, RESULT_TEXT, SPECIALTIES
from app.modules.clinical.catalogue import CATALOGUE

pytestmark = pytest.mark.unit


def test_every_specialty_has_at_least_two_cases():
    assert set(CONTENT) == set(SPECIALTIES)
    assert len(SPECIALTIES) == 10
    assert all(len(cases) >= 2 for cases in CONTENT.values())


def test_every_case_is_signable():
    # A note is signed only with at least one diagnosis and a patient summary
    # (app/modules/clinical/service.py:sign).
    for cases in CONTENT.values():
        for case in cases:
            assert case.diagnoses and case.summary.strip()
            for description, icd10 in case.diagnoses:
                assert 0 < len(description) <= 500 and 0 < len(icd10) <= 10


def test_test_codes_exist_and_have_a_result_text():
    codes = {code for cases in CONTENT.values() for case in cases for code in case.tests}
    assert codes <= set(CATALOGUE)
    assert codes <= set(RESULT_TEXT)


def test_cancellation_reasons_fit_the_column():
    assert CANCELLATION_REASONS and all(len(r) <= 500 for r in CANCELLATION_REASONS)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_content.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.demo'`

- [ ] **Step 3: Create the package and the content**

```python
# app/demo/__init__.py
"""Demo data for a deployed SmartHealth: an imported history plus live activity."""
```

```python
# app/demo/content.py
"""What imported clinical history says: cases per specialty, plain data.

Every specialty of the knowledge corpus (`services/knowledge/corpus/specialties.md`) has
cases, so whatever specialty the assistant names, a patient has been seen there. Test codes
come from `app/modules/clinical/catalogue.py`, so an imported order carries the same
preparation guidance a live one does. Medications are ordinary, widely used ones; this is
demonstration content, not clinical advice.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Medication:
    medication: str
    dose: str
    frequency: str
    duration_days: int
    instructions: str = ""


@dataclass(frozen=True)
class Case:
    reason: str  # what the patient booked for, as the front desk wrote it
    complaint: str
    findings: str
    summary: str  # the patient-facing summary
    diagnoses: tuple[tuple[str, str], ...]  # (description, ICD-10 code)
    medications: tuple[Medication, ...] = ()
    tests: tuple[str, ...] = ()  # codes from the test catalogue


SPECIALTIES: tuple[str, ...] = (
    "Cardiology", "Dermatology", "Endocrinology", "Gastroenterology", "General Practice",
    "Neurology", "Orthopaedics", "Paediatrics", "Physiotherapy", "ENT",
)

CONTENT: dict[str, tuple[Case, ...]] = {
    "Cardiology": (
        Case("Blood pressure review", "Headaches and high home blood-pressure readings.",
             "BP 152/94 seated, repeated 148/92. Heart sounds normal.",
             "Your blood pressure is higher than it should be. We started a daily tablet "
             "and asked for a blood test; please keep a home BP diary.",
             (("Essential hypertension", "I10"),),
             (Medication("Amlodipine", "5 mg", "once daily", 90, "Take in the morning."),),
             ("lipids",)),
        Case("Chest tightness on exertion", "Tightness climbing stairs, settles with rest.",
             "ECG sinus rhythm, no acute changes. BMI 29.",
             "Your heart tracing was normal today. We ordered cholesterol tests and a scan "
             "to look further; call 000 if pain comes at rest.",
             (("Stable angina, under investigation", "I20.8"),),
             (Medication("Atorvastatin", "20 mg", "once daily", 180, "Take in the evening."),),
             ("lipids", "ct")),
    ),
    "Dermatology": (
        Case("Recurring itchy rash", "Itchy dry patches on elbows and hands for months.",
             "Erythematous scaly plaques, flexural, no infection.",
             "This is eczema. Use the cream twice a day for two weeks and moisturise daily.",
             (("Atopic dermatitis", "L20.9"),),
             (Medication("Hydrocortisone 1% cream", "thin layer", "twice daily", 14,
                         "Apply to affected skin only."),)),
        Case("Mole check", "A mole on the back that has changed shape.",
             "6 mm asymmetric pigmented lesion, dermoscopy reassuring.",
             "The mole looks harmless today. We photographed it and will compare in three "
             "months.",
             (("Melanocytic naevus", "D22.5"),)),
    ),
    "Endocrinology": (
        Case("Thyroid review", "Tiredness, weight gain and feeling cold.",
             "Dry skin, pulse 58, no goitre.",
             "Your symptoms may be from an underactive thyroid. We started a tablet and will "
             "recheck your thyroid blood test.",
             (("Hypothyroidism", "E03.9"),),
             (Medication("Levothyroxine", "50 mcg", "once daily", 180,
                         "Take on an empty stomach, 30 minutes before breakfast."),),
             ("thyroid",)),
        Case("Diabetes review", "Thirst and passing more urine.",
             "Random glucose 13.1 mmol/L, BMI 31.",
             "Your sugar levels are high, consistent with type 2 diabetes. We started "
             "metformin and booked blood tests.",
             (("Type 2 diabetes mellitus", "E11.9"),),
             (Medication("Metformin", "500 mg", "twice daily", 180, "Take with meals."),),
             ("hba1c", "fasting-glucose")),
    ),
    "Gastroenterology": (
        Case("Heartburn", "Burning after meals and at night for six weeks.",
             "Epigastric tenderness, no alarm features.",
             "This is acid reflux. Take the tablet before breakfast and avoid late meals.",
             (("Gastro-oesophageal reflux disease", "K21.9"),),
             (Medication("Omeprazole", "20 mg", "once daily", 56,
                         "Take 30 minutes before breakfast."),)),
        Case("Change in bowel habit", "Loose stools and cramps for two months.",
             "Soft abdomen, mild lower tenderness.",
             "We have arranged a colonoscopy to look at the bowel. Follow the preparation "
             "guide carefully.",
             (("Altered bowel habit, under investigation", "R19.4"),), (),
             ("colonoscopy", "fbc")),
    ),
    "General Practice": (
        Case("Cough", "Cough and sore throat for five days.",
             "Temperature 37.6, chest clear, throat red.",
             "This is a viral infection and should settle within two weeks. Rest and drink "
             "fluids; return if you are short of breath.",
             (("Acute upper respiratory infection", "J06.9"),),
             (Medication("Paracetamol", "1 g", "every 6 hours as needed", 5,
                         "No more than 4 g in 24 hours."),)),
        Case("Annual check-up", "Routine check, feels well.",
             "BP 128/80, BMI 26, examination normal.",
             "Everything looked healthy today. We ordered routine blood tests.",
             (("General health examination", "Z00.0"),), (),
             ("fbc", "lipids")),
    ),
    "Neurology": (
        Case("Migraines", "Throbbing one-sided headaches with nausea, twice a month.",
             "Neurological examination normal.",
             "These are migraines. Take the tablet at the start of an attack and keep a "
             "headache diary.",
             (("Migraine without aura", "G43.0"),),
             (Medication("Sumatriptan", "50 mg", "at onset, may repeat after 2 hours", 90,
                         "No more than 2 doses in 24 hours."),)),
        Case("Numbness in hand", "Tingling in the thumb and fingers at night.",
             "Positive Phalen's test, normal power.",
             "This is likely carpal tunnel syndrome. Wear a wrist splint at night; we "
             "ordered a scan.",
             (("Carpal tunnel syndrome", "G56.0"),), (), ("mri",)),
    ),
    "Orthopaedics": (
        Case("Knee pain", "Knee pain on stairs and after running.",
             "Mild effusion, medial joint-line tenderness.",
             "Your knee shows wear in the cartilage. An X-ray and physiotherapy will help.",
             (("Osteoarthritis of the knee", "M17.9"),),
             (Medication("Ibuprofen", "400 mg", "three times daily with food", 10,
                         "Stop if you get stomach pain."),),
             ("xray", "physio")),
        Case("Lower back pain", "Back pain after lifting, no leg symptoms.",
             "Paraspinal spasm, straight-leg raise negative.",
             "This is a back strain. Keep moving gently; it usually settles in a few weeks.",
             (("Low back strain", "S39.012"),), (), ("physio",)),
    ),
    "Paediatrics": (
        Case("Ear pain", "Ear pain and fever since yesterday.",
             "Red bulging left eardrum, temperature 38.2.",
             "Your child has a middle-ear infection. Give the antibiotic for the full course "
             "and paracetamol for pain.",
             (("Acute otitis media", "H66.9"),),
             (Medication("Amoxicillin oral suspension", "dose by weight", "three times daily",
                         5, "Shake well; finish the course."),)),
        Case("Growth check", "Routine growth and development review.",
             "Height and weight on the 50th centile, development normal.",
             "Your child is growing and developing well.",
             (("Routine child health examination", "Z00.1"),)),
    ),
    "Physiotherapy": (
        Case("Shoulder rehabilitation", "Stiff shoulder after a fall six weeks ago.",
             "Reduced abduction to 120 degrees, strength 4/5.",
             "We started an exercise programme. Do the exercises daily and come back in two "
             "weeks.",
             (("Shoulder stiffness after injury", "M25.611"),)),
        Case("Back rehabilitation", "Ongoing lower back stiffness.",
             "Reduced lumbar flexion, core weakness.",
             "We set you core-strength exercises. Little and often works best.",
             (("Chronic low back pain", "M54.5"),)),
    ),
    "ENT": (
        Case("Blocked nose", "Blocked nose and post-nasal drip for months.",
             "Swollen nasal turbinates, clear discharge.",
             "This is rhinitis. Use the nasal spray daily for at least a month.",
             (("Chronic rhinitis", "J31.0"),),
             (Medication("Mometasone nasal spray", "2 sprays each nostril", "once daily", 30,
                         "Aim away from the middle of the nose."),)),
        Case("Sore throats", "Recurrent sore throats this year.",
             "Enlarged tonsils, no current infection.",
             "We will keep a record of throat infections; come back if they continue.",
             (("Recurrent tonsillitis", "J03.91"),)),
    ),
}

CANCELLATION_REASONS: tuple[str, ...] = (
    "Patient called to cancel", "Feeling better", "Work commitment",
    "Booked elsewhere", "Transport problem", "Illness in the family",
)

#: The text of an imported result document, per test code (stored as its extracted text).
RESULT_TEXT: dict[str, str] = {
    "fbc": "Full blood count: haemoglobin, white cells and platelets within normal limits.",
    "fasting-glucose": "Fasting glucose: 7.4 mmol/L (above the normal range).",
    "hba1c": "HbA1c: 58 mmol/mol (7.5%).",
    "lipids": "Lipid profile: total cholesterol 6.1 mmol/L, LDL 3.9 mmol/L.",
    "thyroid": "Thyroid function: TSH 7.8 mU/L (raised), free T4 low-normal.",
    "mri": "MRI: no structural abnormality; findings consistent with the clinical picture.",
    "ct": "CT: no acute abnormality.",
    "xray": "X-ray: mild joint-space narrowing, no fracture.",
    "colonoscopy": "Colonoscopy: no polyps or inflammation seen to the caecum.",
    "physio": "Physiotherapy assessment completed; programme of six sessions agreed.",
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_content.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add app/demo/__init__.py app/demo/content.py tests/tiers/t0_unit/test_demo_content.py
git commit -m "feat(demo): clinical content for imported history, every documented specialty"
```

---

### Task 2: Network constants and pure helpers

**Files:**
- Create: `app/demo/network.py` (constants and pure functions only in this task)
- Test: `tests/tiers/t0_unit/test_demo_network.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tiers/t0_unit/test_demo_network.py
"""The demo network's shape, without a database."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.demo.content import SPECIALTIES
from app.demo.network import (
    CLINICS,
    LINKED_PATIENT_INDEXES,
    MRN_PREFIX,
    PATIENT_COUNT,
    PROVIDERS,
    is_child,
    patient_identity,
    session_starts,
    working,
)

pytestmark = pytest.mark.unit


def test_every_specialty_is_staffed_and_licences_are_unique():
    assert {p.specialty for p in PROVIDERS} == set(SPECIALTIES)
    assert len({p.licence for p in PROVIDERS}) == len(PROVIDERS) == 15
    assert {p.clinic for p in PROVIDERS} == {name for name, _address in CLINICS}


def test_each_provider_holds_two_sessions_a_week_on_weekdays():
    for p in PROVIDERS:
        assert len(set(p.weekdays)) == 2 and all(0 <= d <= 4 for d in p.weekdays)


def test_patient_identity_is_deterministic_and_demo_marked():
    assert patient_identity(42) == patient_identity(42)
    assert patient_identity(0).mrn == f"{MRN_PREFIX}000001"
    emails = {patient_identity(i).email for i in range(PATIENT_COUNT)}
    assert len(emails) == PATIENT_COUNT and all(e.endswith("@example.com") for e in emails)


def test_children_are_children_and_linked_patients_are_adults():
    child = patient_identity(7)
    assert is_child(7) and child.date_of_birth > date(2010, 1, 1)
    assert not any(is_child(i) for i in LINKED_PATIENT_INDEXES)
    assert patient_identity(1).date_of_birth < date(2008, 1, 1)


def test_a_session_is_six_half_hours_from_nine_local_in_utc():
    starts = session_starts(date(2026, 6, 1), ZoneInfo("Australia/Sydney"))
    assert len(starts) == 6 and all(s.tzinfo is UTC for s in starts)
    # 09:00 AEST (UTC+10 in June) is 23:00 UTC the day before.
    assert starts[0] == datetime(2026, 5, 31, 23, 0, tzinfo=UTC)


def test_working_follows_the_weekly_schedule():
    p = PROVIDERS[0]
    monday = date(2026, 6, 1)
    assert working(p, monday) == (0 in p.weekdays)
    assert not working(p, date(2026, 6, 6))  # Saturday
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_network.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.demo.network'`

- [ ] **Step 3: Write the constants and helpers**

```python
# app/demo/network.py
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
        born = date(2011, 1, 1) + timedelta(days=rng.randrange(13 * 365))
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
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_network.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add app/demo/network.py tests/tiers/t0_unit/test_demo_network.py
git commit -m "feat(demo): the network's shape -- 3 clinics, 15 providers, 800 patients, schedules"
```

---

### Task 3: Writing the network (idempotent)

**Files:**
- Modify: `app/demo/network.py` (append `Network`, `build_network`, `ensure_slots`)
- Create: `tests/tiers/t3_integration/demo_helpers.py`
- Test: `tests/tiers/t3_integration/test_demo_data.py`

- [ ] **Step 1: Write the purge helper and the failing test**

```python
# tests/tiers/t3_integration/demo_helpers.py
"""Remove every demo row. The T3 database is shared by the whole run and other tests count
whole tables, so a demo test must leave nothing behind."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.network import (
    CLINICS,
    LICENCE_PREFIX,
    LINKED_PATIENT_INDEXES,
    MRN_PREFIX,
    PROVIDERS,
    STAFF_LOGINS,
    patient_identity,
)

_PATIENTS = "SELECT id FROM patients WHERE mrn LIKE :mrn"
_PROVIDERS = "SELECT id FROM providers WHERE license_number LIKE :lic"
_APPOINTMENTS = f"SELECT id FROM appointments WHERE provider_id IN ({_PROVIDERS})"


async def purge_demo(session: AsyncSession) -> None:
    params = {"mrn": f"{MRN_PREFIX}%", "lic": f"{LICENCE_PREFIX}%",
              "clinics": [name for name, _ in CLINICS],
              "emails": [e for e, _ in STAFF_LOGINS] + [p.email for p in PROVIDERS]
              + [patient_identity(i).email for i in LINKED_PATIENT_INDEXES]}
    statements = (
        f"DELETE FROM test_orders WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM prescriptions WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM clinical_notes WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM patient_documents WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM notifications WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM billing_records WHERE appointment_id IN ({_APPOINTMENTS})",
        f"DELETE FROM visits WHERE appointment_id IN ({_APPOINTMENTS})",
        f"DELETE FROM waitlist_entries WHERE patient_id IN ({_PATIENTS})",
        f"DELETE FROM appointments WHERE provider_id IN ({_PROVIDERS})",
        f"DELETE FROM provider_slots WHERE provider_id IN ({_PROVIDERS})",
        "DELETE FROM analytics_daily WHERE clinic_id IN "
        "(SELECT id FROM clinics WHERE name = ANY(:clinics))",
        f"DELETE FROM provider_departments WHERE provider_id IN ({_PROVIDERS})",
        "DELETE FROM providers WHERE license_number LIKE :lic",
        "DELETE FROM patients WHERE mrn LIKE :mrn",
        "DELETE FROM departments WHERE clinic_id IN "
        "(SELECT id FROM clinics WHERE name = ANY(:clinics))",
        "DELETE FROM clinics WHERE name = ANY(:clinics)",
        # Exactly the demo's logins, never a pattern: other tests own @example.com users.
        "DELETE FROM users WHERE email = ANY(:emails)",
    )
    for statement in statements:
        await session.execute(text(statement), params)
    await session.commit()
```

> The user delete names exact addresses (staff, providers, the five linked patients). Never
> widen it to a pattern: `@example.com` users belong to other tests too.

```python
# tests/tiers/t3_integration/test_demo_data.py
"""Demo data against a real database: every constraint holds, re-runs add nothing, the
analytics reconcile finds no drift, and the live phase books through real workflows."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.clock import Clock
from app.core.ratelimit import InMemoryRateLimiter
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.demo.network import (
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    build_network,
    ensure_slots,
    patient_identity,
)
from app.modules.identity.service import authenticate
from app.modules.identity.sessions import InMemorySessionStore
from app.modules.patients.models import Patient
from app.modules.scheduling.models import ProviderSlot
from tests.tiers.t3_integration.demo_helpers import purge_demo

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PASSWORD = "demo-integration-password"


@pytest.fixture
async def demo_factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        await purge_demo(session)
    try:
        yield factory
    finally:
        async with factory() as session:
            await purge_demo(session)
        await engine.dispose()


async def test_the_network_is_idempotent_and_its_logins_authenticate(demo_factory, db_settings):
    async with demo_factory() as session:
        net, created = await build_network(session, password=PASSWORD)
        await session.commit()
    assert created["providers"] == len(PROVIDERS) and created["patients"] == PATIENT_COUNT
    async with demo_factory() as session:
        again, created_again = await build_network(session, password=PASSWORD)
        await session.commit()
    assert set(created_again.values()) == {0}
    assert again.provider_ids == net.provider_ids
    async with demo_factory() as session:
        email = patient_identity(LINKED_PATIENT_INDEXES[0]).email
        tokens = await authenticate(
            session, email=email, password=PASSWORD, client_ip="127.0.0.1",
            sessions=InMemorySessionStore(), limiter=InMemoryRateLimiter(),
            settings=db_settings, now=Clock().now(),
        )
        assert tokens.access_token
        linked = await session.scalar(
            select(func.count()).select_from(Patient).where(Patient.user_id.is_not(None),
                                                            Patient.mrn.like("DEMO-%")))
        assert linked == len(LINKED_PATIENT_INDEXES)


async def test_upcoming_slots_follow_the_schedule_and_are_not_duplicated(demo_factory):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        today = Clock().now().date()
        made = await ensure_slots(session, net, first_day=today,
                                  last_day=today + timedelta(days=13))
        await session.commit()
        assert made > 0
        again = await ensure_slots(session, net, first_day=today,
                                   last_day=today + timedelta(days=13))
        assert again == 0
        count = await session.scalar(select(func.count()).select_from(ProviderSlot).where(
            ProviderSlot.provider_id.in_(net.provider_ids.values())))
        assert count == made
```

The `authenticate` call above is the real signature (`app/modules/identity/service.py:67`),
the same call `tests/tiers/t3_integration/test_seed.py` makes for a seeded login.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker`
Expected: FAIL with `ImportError: cannot import name 'build_network'`

- [ ] **Step 3: Append the writers to `app/demo/network.py`**

```python
# --- appended to app/demo/network.py -------------------------------------------------


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

    existing = {row.mrn: row for row in (await session.scalars(
        select(Patient).where(Patient.mrn.like(f"{MRN_PREFIX}%")))).all()}
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
    yet. Inserts only -- never an update -- so it stays clear of the slot-write guard."""
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
```

> `provider.user_id = …` and `patients[index].user_id = …` are attribute assignments, but not
> to `status`, so the slot-write guard is satisfied. Do not add any `.status =` in this file.

- [ ] **Step 4: Run to verify it passes**

Bring up the test stack if needed (`test-stack` skill), then:
Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker`
Expected: `2 passed`
Then: `python -m pytest tests/meta/test_slot_writes.py -q` → passes.

- [ ] **Step 5: Commit**

```bash
git add app/demo/network.py tests/tiers/t3_integration/demo_helpers.py tests/tiers/t3_integration/test_demo_data.py
git commit -m "feat(demo): build the network idempotently, with logins and upcoming slots"
```

---

### Task 4: Planning a day of history (pure)

**Files:**
- Create: `app/demo/history.py` (planning part)
- Test: `tests/tiers/t0_unit/test_demo_history.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tiers/t0_unit/test_demo_history.py
"""History planning without a database: deterministic, plausible, and never overlapping."""

from __future__ import annotations

import io
from collections import Counter
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.demo.history import fill_rate, plan_day, result_pdf
from app.demo.network import PROVIDERS_BY_LICENCE, is_child

pytestmark = pytest.mark.unit

TZ = ZoneInfo("Australia/Sydney")
TODAY = date(2026, 11, 16)


def _days(n: int) -> list[date]:
    return [TODAY - timedelta(days=d) for d in range(1, n + 1)]


def test_the_same_day_always_plans_the_same():
    day = date(2026, 9, 7)
    assert plan_day(day, tz=TZ, today=TODAY) == plan_day(day, tz=TZ, today=TODAY)


def test_weekends_are_closed():
    saturday = date(2026, 9, 12)
    plan = plan_day(saturday, tz=TZ, today=TODAY)
    assert plan.slots == () and plan.appointments == ()


def test_fill_rate_stays_within_bounds():
    for days_back in range(0, 200, 7):
        for specialty in ("General Practice", "Neurology"):
            assert 0.55 <= fill_rate(specialty, TODAY - timedelta(days=days_back), TODAY) <= 0.80


def test_outcomes_over_two_months_are_close_to_the_target_shares():
    outcomes = Counter(a.outcome for day in _days(60)
                       for a in plan_day(day, tz=TZ, today=TODAY).appointments)
    total = sum(outcomes.values())
    assert total > 300
    assert 0.76 <= outcomes["completed"] / total <= 0.88
    assert 0.06 <= outcomes["cancelled"] / total <= 0.14
    assert 0.04 <= outcomes["no_show"] / total <= 0.12


def test_no_patient_has_two_appointments_on_one_day():
    for day in _days(30):
        patients = [a.patient_index for a in plan_day(day, tz=TZ, today=TODAY).appointments]
        assert len(patients) == len(set(patients))


def test_paediatrics_sees_only_children_and_others_only_adults():
    for day in _days(30):
        for a in plan_day(day, tz=TZ, today=TODAY).appointments:
            child = is_child(a.patient_index)
            assert child == (PROVIDERS_BY_LICENCE[a.licence].specialty == "Paediatrics")


def test_times_are_in_order_and_waits_are_five_to_forty_minutes():
    for day in _days(20):
        for a in plan_day(day, tz=TZ, today=TODAY).appointments:
            assert a.confirmed_at < a.starts_at
            if a.outcome == "cancelled":
                assert a.confirmed_at < a.cancelled_at < a.starts_at and a.cancellation_reason
            if a.outcome == "completed":
                v = a.visit
                assert v.checked_in_at <= a.starts_at <= v.seen_at + timedelta(minutes=45)
                wait = (v.seen_at - v.checked_in_at).total_seconds() / 60
                assert 5 <= wait <= 40 and v.completed_at > v.seen_at


def test_a_result_pdf_reads_back_as_its_text():
    from pypdf import PdfReader

    data = result_pdf("HbA1c: 58 mmol/mol (7.5%).")
    assert data.startswith(b"%PDF-")
    text = PdfReader(io.BytesIO(data)).pages[0].extract_text()
    assert "HbA1c" in text and "(7.5%)" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_history.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.demo.history'`

- [ ] **Step 3: Write the planning half of `app/demo/history.py`**

```python
# app/demo/history.py
"""Imported history: what MediNova's previous system would have handed over.

Two halves, deliberately separate:

- **planning** (`plan_day`) is pure: a random generator seeded with the date decides every
  slot, booking and outcome, so the same day always produces the same history and the rules
  are unit-tested without a database;
- **importing** (`import_day`) writes one planned day: slots inserted with their final
  status, appointments marked `origin = 'legacy_import'`, visits, signed notes,
  prescriptions, test orders and result documents, billing, and the day's analytics -- no
  outbox events (no emails about the past), no workflows, no per-row audit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.demo.content import CANCELLATION_REASONS, CONTENT
from app.demo.network import (
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    DemoProvider,
    is_child,
    session_starts,
    working,
)

ORIGIN = "legacy_import"
HISTORY_DAYS = 182
COMPLETED_SHARE = 0.82
CANCELLED_SHARE = 0.10  # the rest are no-shows
BUSY = frozenset({"General Practice", "Paediatrics", "Physiotherapy"})
LINKED_WEIGHT = 6  # the five patients with logins get rich histories


@dataclass(frozen=True)
class PlannedVisit:
    checked_in_at: datetime
    seen_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class PlannedAppointment:
    licence: str
    patient_index: int
    starts_at: datetime
    outcome: str  # "completed" | "cancelled" | "no_show"
    confirmed_at: datetime
    cancelled_at: datetime | None
    cancellation_reason: str | None
    visit: PlannedVisit | None
    case: int  # index into CONTENT[specialty]
    prescribe: bool
    order_test: bool


@dataclass(frozen=True)
class PlannedDay:
    day: date
    slots: tuple[tuple[str, datetime], ...]  # (licence, starts_at UTC)
    appointments: tuple[PlannedAppointment, ...]


def fill_rate(specialty: str, day: date, today: date) -> float:
    """Share of a session's slots booked: busier for GP, Paediatrics and Physiotherapy,
    Mondays up and Fridays down, and a slow rise towards today -- a trend worth charting."""
    rate = 0.66 + (0.08 if specialty in BUSY else 0.0)
    rate += {0: 0.04, 4: -0.04}.get(day.weekday(), 0.0)
    rate -= 0.012 * ((today - day).days / 30)
    return min(0.80, max(0.55, rate))


_ADULTS = tuple(i for i in range(PATIENT_COUNT) if not is_child(i))
_CHILDREN = tuple(i for i in range(PATIENT_COUNT) if is_child(i))


def plan_day(day: date, *, tz: ZoneInfo, today: date) -> PlannedDay:
    rng = random.Random(f"smarthealth-demo:{day.isoformat()}")
    used: set[int] = set()
    slots: list[tuple[str, datetime]] = []
    appointments: list[PlannedAppointment] = []
    for provider in PROVIDERS:
        if not working(provider, day):
            continue
        rate = fill_rate(provider.specialty, day, today)
        for position, start in enumerate(session_starts(day, tz)):
            slots.append((provider.licence, start))
            if rng.random() >= rate:
                continue
            pool = [i for i in (_CHILDREN if provider.specialty == "Paediatrics" else _ADULTS)
                    if i not in used]
            weights = [LINKED_WEIGHT if i in LINKED_PATIENT_INDEXES else 1 for i in pool]
            patient = rng.choices(pool, weights=weights)[0]
            used.add(patient)
            appointments.append(_outcome(rng, provider, patient, start, position))
    return PlannedDay(day, tuple(slots), tuple(appointments))


def _outcome(rng: random.Random, provider: DemoProvider, patient: int, start: datetime,
             position: int) -> PlannedAppointment:
    cases = CONTENT[provider.specialty]
    case = rng.randrange(len(cases))
    confirmed = start - timedelta(days=rng.randint(1, 21), minutes=rng.randint(0, 600))
    roll = rng.random()
    if roll < COMPLETED_SHARE:
        checked_in = start - timedelta(minutes=rng.randint(0, 12))
        wait = rng.randint(5, 25) + 3 * position  # the morning backs up
        seen = checked_in + timedelta(minutes=wait)
        completed = seen + timedelta(minutes=rng.randint(10, 25))
        return PlannedAppointment(
            provider.licence, patient, start, "completed", confirmed, None, None,
            PlannedVisit(checked_in, seen, completed), case,
            prescribe=bool(cases[case].medications) and rng.random() < 0.40,
            order_test=bool(cases[case].tests) and rng.random() < 0.25)
    if roll < COMPLETED_SHARE + CANCELLED_SHARE:
        cancelled = confirmed + (start - confirmed) * rng.uniform(0.3, 0.98)
        return PlannedAppointment(
            provider.licence, patient, start, "cancelled", confirmed, cancelled,
            rng.choice(CANCELLATION_REASONS), None, case, False, False)
    return PlannedAppointment(provider.licence, patient, start, "no_show", confirmed, None,
                              None, None, case, False, False)


def result_pdf(text: str) -> bytes:
    """A minimal one-page PDF carrying `text` -- enough for the document viewer and pypdf."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 11 Tf 50 780 Td ({safe}) Tj ET".encode("latin-1", "replace")
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1, xref)
    return bytes(out)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_history.py -q`
Expected: `8 passed`. If the outcome-share test fails narrowly, widen nothing — check that
`_outcome` consumes `rng` in the order shown (determinism depends on it).

- [ ] **Step 5: Commit**

```bash
git add app/demo/history.py tests/tiers/t0_unit/test_demo_history.py
git commit -m "feat(demo): plan a day of history -- deterministic, plausible, never overlapping"
```

---

### Task 5: Importing a planned day

**Files:**
- Modify: `app/demo/history.py` (append the importing half)
- Modify: `tests/tiers/t3_integration/test_demo_data.py` (append tests)

- [ ] **Step 1: Append the failing tests**

```python
# --- appended to tests/tiers/t3_integration/test_demo_data.py ------------------------
from app.demo.history import ORIGIN, day_imported, import_day, last_imported_day, plan_day
from app.modules.analytics.tasks import reconcile
from app.modules.billing.models import BillingRecord, BillingStatus
from app.modules.clinical.models import ClinicalNote, NoteStatus, PatientDocument
from app.modules.clinical.storage import FilesystemDocumentStore
from app.modules.scheduling.models import Appointment, AppointmentStatus, Visit


async def _import(demo_factory, db_settings, days):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        await session.commit()
    store = FilesystemDocumentStore.from_settings(db_settings)
    today = Clock().now().astimezone(net.tz).date()
    for day in days:
        async with demo_factory() as session:
            if await day_imported(session, net, day):
                continue
            await import_day(session, net, plan_day(day, tz=net.tz, today=today),
                             store=store, settings=db_settings, now=Clock().now())
            await session.commit()
    return net, today


async def test_imported_history_holds_every_rule_and_is_marked(demo_factory, db_settings):
    today = Clock().now().date()
    days = [today - timedelta(days=d) for d in range(30, 20, -1)]
    net, _ = await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        ids = list(net.provider_ids.values())
        appts = (await session.scalars(select(Appointment).where(
            Appointment.provider_id.in_(ids)))).all()
        assert appts and {a.origin for a in appts} == {ORIGIN}
        assert {a.status for a in appts} <= {AppointmentStatus.COMPLETED,
                                             AppointmentStatus.CANCELLED,
                                             AppointmentStatus.NO_SHOW}
        completed = [a for a in appts if a.status is AppointmentStatus.COMPLETED]
        visits = (await session.scalars(select(Visit).where(
            Visit.appointment_id.in_([a.id for a in completed])))).all()
        assert len(visits) == len(completed)
        notes = (await session.scalars(select(ClinicalNote).where(
            ClinicalNote.appointment_id.in_([a.id for a in completed])))).all()
        assert len(notes) == len(completed) and {n.status for n in notes} == {NoteStatus.SIGNED}
        billing = {b.appointment_id: b.status for b in (await session.scalars(
            select(BillingRecord).where(BillingRecord.appointment_id.in_([a.id for a in appts]))
        )).all()}
        assert len(billing) == len(appts)
        for a in appts:
            expected = (BillingStatus.CAPTURED if a.status is AppointmentStatus.COMPLETED
                        else BillingStatus.VOIDED)
            assert billing[a.id] is expected
        docs = (await session.scalars(select(PatientDocument).where(
            PatientDocument.patient_id.in_(net.patient_ids)))).all()
        assert all(d.extraction_status == "extracted" and d.extracted_text for d in docs)


async def test_importing_again_adds_nothing(demo_factory, db_settings):
    today = Clock().now().date()
    days = [today - timedelta(days=d) for d in range(15, 10, -1)]
    net, _ = await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        before = await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values())))
    await _import(demo_factory, db_settings, days)
    async with demo_factory() as session:
        after = await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values())))
        assert after == before
        assert await last_imported_day(session, net) == max(
            d for d in days if d.weekday() < 5)


async def test_the_nightly_reconcile_finds_no_drift_in_imported_days(demo_factory, db_settings):
    today = Clock().now().date()
    days = [today - timedelta(days=d) for d in range(40, 9, -1)]
    net, _ = await _import(demo_factory, db_settings, days)
    # Bookings count on their confirmation day, up to 21 days before the visit, so check a
    # window that starts after the earliest imported confirmation could fall outside it.
    drift = await reconcile(db_settings, start=today - timedelta(days=19),
                            end=today - timedelta(days=11))
    demo_clinics = {str(c) for c in net.clinic_ids.values()}
    assert [d for d in drift if d["clinic_id"] in demo_clinics] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker`
Expected: FAIL with `ImportError: cannot import name 'day_imported'`

- [ ] **Step 3: Append the importing half to `app/demo/history.py`**

Add these imports at the top of the file (merge with the existing import block):

```python
import hashlib
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.content import RESULT_TEXT
from app.demo.network import PROVIDERS_BY_LICENCE, SLOT_DURATION, Network
from app.modules.analytics import service as analytics_service
from app.modules.billing.models import BillingRecord, BillingStatus
from app.modules.clinical.models import (
    ClinicalNote,
    NoteStatus,
    OrderStatus,
    PatientDocument,
    Prescription,
    PrescriptionStatus,
    TestOrder,
)
from app.modules.clinical.storage import DocumentStore
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    SlotStatus,
    Visit,
    VisitStatus,
)
from app.settings import Settings
```

Then append:

```python
NO_SHOW_GRACE = timedelta(minutes=15)  # when the live visit timer would have marked it
REVIEW_AFTER = timedelta(days=14)
ACTOR = "system:demo-import"

_STATUS = {"completed": AppointmentStatus.COMPLETED, "cancelled": AppointmentStatus.CANCELLED,
           "no_show": AppointmentStatus.NO_SHOW}
_BILLING = {"completed": BillingStatus.CAPTURED, "cancelled": BillingStatus.VOIDED,
            "no_show": BillingStatus.VOIDED}


@dataclass
class DayCounts:
    slots: int = 0
    appointments: int = 0
    completed: int = 0
    cancelled: int = 0
    no_show: int = 0
    prescriptions: int = 0
    orders: int = 0
    documents: int = 0

    def add(self, other: DayCounts) -> None:
        for name, value in asdict(other).items():
            setattr(self, name, getattr(self, name) + value)


def _local_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


async def day_imported(session: AsyncSession, net: Network, day: date) -> bool:
    """A day is imported once any demo provider has a slot on it (history writes every
    session's slots, booked or not; upcoming slots never reach the past)."""
    lower, upper = _local_bounds(day, net.tz)
    found = await session.scalar(select(ProviderSlot.id).where(
        ProviderSlot.provider_id.in_(net.provider_ids.values()),
        ProviderSlot.starts_at >= lower, ProviderSlot.starts_at < upper).limit(1))
    return found is not None


async def last_imported_day(session: AsyncSession, net: Network) -> date | None:
    """The latest past day holding a demo slot, in the clinics' timezone."""
    today_start, _ = _local_bounds(datetime.now(net.tz).date(), net.tz)
    latest = await session.scalar(select(func.max(ProviderSlot.starts_at)).where(
        ProviderSlot.provider_id.in_(net.provider_ids.values()),
        ProviderSlot.starts_at < today_start))
    return latest.astimezone(net.tz).date() if latest else None


def _utc_day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


async def import_day(session: AsyncSession, net: Network, plan: PlannedDay, *,
                     store: DocumentStore, settings: Settings, now: datetime) -> DayCounts:
    """Write one planned day. Flushes, never commits: the caller commits per day, so a crash
    leaves whole days imported or not, and `day_imported` skips the finished ones."""
    counts = DayCounts()
    booked = {(a.licence, a.starts_at) for a in plan.appointments if a.outcome != "cancelled"}
    slots: dict[tuple[str, datetime], ProviderSlot] = {}
    for licence, start in plan.slots:
        meta = PROVIDERS_BY_LICENCE[licence]
        slot = ProviderSlot(
            provider_id=net.provider_ids[licence], clinic_id=net.clinic_ids[meta.clinic],
            department_id=net.department_ids[(meta.clinic, meta.specialty)],
            starts_at=start, ends_at=start + SLOT_DURATION,
            status=SlotStatus.BOOKED if (licence, start) in booked else SlotStatus.FREE)
        session.add(slot)
        slots[(licence, start)] = slot
    counts.slots = len(slots)
    await session.flush()

    rows: list[tuple[PlannedAppointment, Appointment]] = []
    for planned in plan.appointments:
        meta = PROVIDERS_BY_LICENCE[planned.licence]
        appointment = Appointment(
            patient_id=net.patient_ids[planned.patient_index],
            provider_id=net.provider_ids[planned.licence],
            clinic_id=net.clinic_ids[meta.clinic],
            department_id=net.department_ids[(meta.clinic, meta.specialty)],
            slot_id=slots[(planned.licence, planned.starts_at)].id,
            status=_STATUS[planned.outcome],
            reason=CONTENT[meta.specialty][planned.case].reason,
            origin=ORIGIN, confirmed_at=planned.confirmed_at,
            cancelled_at=planned.cancelled_at,
            cancellation_reason=planned.cancellation_reason,
            created_at=planned.confirmed_at)
        session.add(appointment)
        rows.append((planned, appointment))
    await session.flush()

    analytics: dict[tuple[date, uuid.UUID, uuid.UUID], Counter] = defaultdict(Counter)
    visits: list[tuple[PlannedAppointment, Appointment, Visit]] = []
    for planned, appointment in rows:
        key_ids = (appointment.clinic_id, appointment.provider_id)
        analytics[(_utc_day(planned.confirmed_at), *key_ids)]["booked"] += 1
        session.add(BillingRecord(
            appointment_id=appointment.id, patient_id=appointment.patient_id,
            status=_BILLING[planned.outcome], amount_minor=settings.billing_amount_minor,
            currency=settings.billing_currency, authorization_ref=f"legacy-{appointment.id}"))
        counts.appointments += 1
        if planned.outcome == "cancelled":
            counts.cancelled += 1
            analytics[(_utc_day(planned.cancelled_at), *key_ids)]["cancelled"] += 1
        elif planned.outcome == "no_show":
            counts.no_show += 1
            marked = planned.starts_at + SLOT_DURATION + NO_SHOW_GRACE
            analytics[(_utc_day(marked), *key_ids)]["no_show"] += 1
        else:
            counts.completed += 1
            v = planned.visit
            visit = Visit(appointment_id=appointment.id, checked_in_at=v.checked_in_at,
                          seen_at=v.seen_at, completed_at=v.completed_at,
                          status=VisitStatus.COMPLETED)
            session.add(visit)
            visits.append((planned, appointment, visit))
            day_key = (_utc_day(v.completed_at), *key_ids)
            analytics[day_key]["completed"] += 1
            analytics[day_key]["wait_seconds_sum"] += int((v.seen_at - v.checked_in_at)
                                                          .total_seconds())
            analytics[day_key]["wait_count"] += 1
    await session.flush()

    for planned, appointment, visit in visits:
        meta = PROVIDERS_BY_LICENCE[planned.licence]
        case = CONTENT[meta.specialty][planned.case]
        prescriptions = list(case.medications) if planned.prescribe else []
        tests = list(case.tests[:1]) if planned.order_test else []
        note = ClinicalNote(
            visit_id=visit.id, appointment_id=appointment.id,
            patient_id=appointment.patient_id, provider_id=appointment.provider_id,
            version=1, status=NoteStatus.SIGNED,
            presenting_complaint=case.complaint, findings=case.findings, clinician_notes="",
            patient_summary=case.summary,
            diagnoses=[{"description": d, "icd10": c} for d, c in case.diagnoses],
            draft_prescriptions=[{"medication": m.medication, "dose": m.dose,
                                  "frequency": m.frequency, "duration": f"{m.duration_days} days",
                                  "instructions": m.instructions} for m in prescriptions],
            draft_orders=[{"test_code": code, "reason": case.reason, "urgency": "routine"}
                          for code in tests],
            signed_at=planned.visit.completed_at - timedelta(minutes=2),
            signed_by=str(net.provider_user_ids[planned.licence]))
        session.add(note)
        await session.flush()
        completed_at = planned.visit.completed_at
        for m in prescriptions:
            still_taking = completed_at + timedelta(days=m.duration_days) > now
            session.add(Prescription(
                note_id=note.id, patient_id=appointment.patient_id, medication=m.medication,
                dose=m.dose, frequency=m.frequency, duration=f"{m.duration_days} days",
                instructions=m.instructions,
                status=PrescriptionStatus.ACTIVE if still_taking else PrescriptionStatus.COMPLETED))
            counts.prescriptions += 1
        for code in tests:
            reviewed = completed_at < now - REVIEW_AFTER
            document_id = None
            if reviewed:
                document_id = await _result_document(
                    session, store, appointment.patient_id, code, completed_at)
                counts.documents += 1
            session.add(TestOrder(
                note_id=note.id, patient_id=appointment.patient_id,
                ordered_by=appointment.provider_id, test_code=code, reason=case.reason,
                urgency="routine",
                status=OrderStatus.REVIEWED if reviewed else OrderStatus.ORDERED,
                result_document_id=document_id,
                reviewed_at=completed_at + timedelta(days=7) if reviewed else None))
            counts.orders += 1
    await session.flush()

    for (day, clinic_id, provider_id), increments in analytics.items():
        await analytics_service.record(session, day=day, clinic_id=clinic_id,
                                       provider_id=provider_id, **dict(increments))
    return counts


async def _result_document(session: AsyncSession, store: DocumentStore, patient_id: uuid.UUID,
                           code: str, completed_at: datetime) -> uuid.UUID:
    text = RESULT_TEXT[code]
    data = result_pdf(text)
    key = f"patients/{patient_id}/{uuid.uuid4()}"
    await store.put(key, data, "application/pdf")
    document = PatientDocument(
        patient_id=patient_id, uploaded_by=ACTOR, filename=f"{code}-result.pdf",
        media_type="application/pdf", size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(), object_key=key,
        extraction_status="extracted", extracted_text=text,
        created_at=completed_at + timedelta(days=3))
    session.add(document)
    await session.flush()
    return document.id
```

> `test_order_id` on the document is left `NULL`: the order row is created after the document
> (the order needs the document's id). The patient record links them through
> `TestOrder.result_document_id`, which is what the review flow reads.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker`
Expected: `5 passed`
Then: `python -m pytest tests/meta/test_slot_writes.py tests/meta/test_import_boundaries.py -q` → passes.

If `test_the_nightly_reconcile_finds_no_drift_in_imported_days` reports drift, print it: the
cause is almost always a local-versus-UTC day mismatch — every analytics key above must come
from `_utc_day(...)`.

- [ ] **Step 5: Commit**

```bash
git add app/demo/history.py tests/tiers/t3_integration/test_demo_data.py
git commit -m "feat(demo): import a planned day -- marked history, signed notes, billing, zero-drift analytics"
```

---

### Task 6: Live activity through the API

**Files:**
- Create: `app/demo/live.py`
- Modify: `tests/tiers/t3_integration/test_demo_data.py` (append the live test)

- [ ] **Step 1: Append the failing test**

```python
# --- appended to tests/tiers/t3_integration/test_demo_data.py ------------------------
from app.components import build_activities, register_all  # move to the top imports
from app.core.audit import AuditLog
from app.core.temporal import TASK_QUEUE, WORKFLOWS, build_worker, connect_client
from app.db.mongo import create_mongo_client, get_audit_collection
from app.demo.live import run_live


@pytest.fixture
async def demo_worker(db_settings, demo_factory):
    register_all()
    mongo = create_mongo_client(db_settings)
    audit = AuditLog(get_audit_collection(mongo, db_settings), Clock())
    client = await connect_client(db_settings, lazy=False)
    worker = build_worker(
        client, db_settings.task_queue(TASK_QUEUE), workflows=list(WORKFLOWS),
        activities=build_activities(demo_factory, audit, Clock(), timedelta(minutes=15),
                                    settings=db_settings))
    async with worker:
        yield
    await mongo.close()


async def test_live_activity_goes_through_the_real_workflows(api, demo_factory, demo_worker):
    async with demo_factory() as session:
        net, _ = await build_network(session, password=PASSWORD)
        today = Clock().now().astimezone(net.tz).date()
        await ensure_slots(session, net, first_day=today + timedelta(days=1),
                           last_day=today + timedelta(days=14))
        await session.commit()
    summary = await run_live(api, net, password=PASSWORD, target=4, existing_upcoming=0,
                             existing_waitlist=0, now=Clock().now())
    assert summary.booked >= 3 and summary.cancelled == 1 and summary.rescheduled == 1
    assert summary.waitlisted == 2
    async with demo_factory() as session:
        live = (await session.scalars(select(Appointment).where(
            Appointment.provider_id.in_(net.provider_ids.values()),
            Appointment.origin.in_(("front_desk", "web"))))).all()
        statuses = Counter(a.status for a in live)
        assert statuses[AppointmentStatus.CONFIRMED] >= 2
        assert statuses[AppointmentStatus.CANCELLED] == 1
        assert statuses[AppointmentStatus.RESCHEDULED] == 1
```

Add `from collections import Counter` to the test file's imports.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker -k live`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.demo.live'`

- [ ] **Step 3: Write `app/demo/live.py`**

```python
# app/demo/live.py
"""Live demo activity: real people doing real things through Part A's HTTP API.

Every booking here starts a real `BookAppointmentWorkflow`, writes real events and sends real
(Mailpit) emails -- so a reviewer finds workflow histories, traces and messages, not just
rows. Bookings are spread over two front-desk logins and the linked patients, and a 429 is
waited out (bookings are limited per user per minute).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from app.demo.network import (
    LINKED_PATIENT_INDEXES,
    PATIENT_COUNT,
    PROVIDERS,
    STAFF_LOGINS,
    UPCOMING_DAYS,
    Network,
    is_child,
    patient_identity,
)

TARGET_UPCOMING = 20
TARGET_WAITLIST = 2
WALK_IN_START = 600  # walk-in patients booked by the front desk come from here on
_CONFIRM_TIMEOUT = 60.0


@dataclass
class LiveSummary:
    booked: int = 0
    cancelled: int = 0
    rescheduled: int = 0
    waitlisted: int = 0


class LiveClient:
    def __init__(self, http: httpx.AsyncClient, password: str) -> None:
        self._http = http
        self._password = password
        self._tokens: dict[str, dict[str, str]] = {}

    async def headers(self, email: str) -> dict[str, str]:
        if email not in self._tokens:
            response = await self._http.post(
                "/auth/login", json={"email": email, "password": self._password})
            response.raise_for_status()
            self._tokens[email] = {"Authorization":
                                   f"Bearer {response.json()['access_token']}"}
        return self._tokens[email]

    async def _send(self, method: str, url: str, email: str, **kwargs) -> httpx.Response:
        for _ in range(6):
            response = await self._http.request(method, url, headers={
                **(await self.headers(email)), **kwargs.pop("extra_headers", {})}, **kwargs)
            if response.status_code != 429:
                return response
            await asyncio.sleep(int(response.headers.get("Retry-After", "5")))
        return response

    async def free_slots(self, email: str, provider_id: uuid.UUID, start: datetime,
                         end: datetime) -> list[dict]:
        response = await self._send("GET", "/slots", email, params={
            "provider_id": str(provider_id), "from": start.isoformat(), "to": end.isoformat(),
            "limit": 200})
        response.raise_for_status()
        return response.json()["items"]

    async def book(self, email: str, patient_id: uuid.UUID, slot_id: str) -> str:
        response = await self._send(
            "POST", "/appointments", email,
            json={"patient_id": str(patient_id), "slot_id": slot_id,
                  "reason": "Booked for the demo"},
            extra_headers={"Idempotency-Key": f"demo-{uuid.uuid4()}"})
        response.raise_for_status()
        return response.json()["appointment_id"]

    async def wait(self, email: str, appointment_id: str) -> str:
        deadline = asyncio.get_running_loop().time() + _CONFIRM_TIMEOUT
        while True:
            response = await self._send("GET", f"/appointments/{appointment_id}", email)
            response.raise_for_status()
            status = response.json()["status"]
            if status != "pending":
                return status
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"appointment {appointment_id} still pending")
            await asyncio.sleep(0.5)

    async def cancel(self, email: str, appointment_id: str, reason: str) -> None:
        response = await self._send("POST", f"/appointments/{appointment_id}/cancel", email,
                                    json={"reason": reason})
        response.raise_for_status()

    async def reschedule(self, email: str, appointment_id: str, new_slot_id: str) -> str:
        response = await self._send(
            "POST", f"/appointments/{appointment_id}/reschedule", email,
            json={"new_slot_id": new_slot_id, "reason": "Patient asked to move it"},
            extra_headers={"Idempotency-Key": f"demo-{uuid.uuid4()}"})
        response.raise_for_status()
        return response.json()["appointment_id"]

    async def join_waitlist(self, email: str, patient_id: uuid.UUID, provider_id: uuid.UUID,
                            start: datetime, end: datetime) -> None:
        response = await self._send("POST", "/waitlist", email, json={
            "patient_id": str(patient_id), "provider_id": str(provider_id),
            "window_start": start.isoformat(), "window_end": end.isoformat()})
        response.raise_for_status()


async def run_live(http: httpx.AsyncClient, net: Network, *, password: str, now: datetime,
                   target: int = TARGET_UPCOMING, existing_upcoming: int = 0,
                   existing_waitlist: int = 0) -> LiveSummary:
    """Book up to `target` upcoming appointments, then cancel one and reschedule one of the
    new bookings, and top the waitlist up to `TARGET_WAITLIST` entries."""
    client = LiveClient(http, password)
    summary = LiveSummary()
    desks = [email for email, role in STAFF_LOGINS if role.value == "front_desk"]
    adult_providers = [p for p in PROVIDERS if p.specialty != "Paediatrics"]
    start, end = now + timedelta(days=1), now + timedelta(days=UPCOMING_DAYS)
    walk_ins = [i for i in range(WALK_IN_START, PATIENT_COUNT) if not is_child(i)]

    bookers: list[tuple[str, int]] = [
        (patient_identity(i).email, i) for i in LINKED_PATIENT_INDEXES]
    bookers += [(desks[n % len(desks)], walk_ins[(existing_upcoming + n) % len(walk_ins)])
                for n in range(target)]
    made: list[tuple[str, uuid.UUID]] = []  # (appointment id, provider id)
    for n, (email, patient_index) in enumerate(bookers[:max(0, target - existing_upcoming)]):
        provider = adult_providers[n % len(adult_providers)]
        provider_id = net.provider_ids[provider.licence]
        slots = await client.free_slots(email, provider_id, start, end)
        if not slots:
            continue
        slot = slots[(n * 3) % len(slots)]
        appointment_id = await client.book(email, net.patient_ids[patient_index], slot["id"])
        if await client.wait(email, appointment_id) == "confirmed":
            summary.booked += 1
            made.append((appointment_id, provider_id))

    if len(made) >= 3:
        await client.cancel(desks[0], made[0][0], "Patient called to cancel")
        summary.cancelled = 1
        appointment_id, provider_id = made[1]
        slots = await client.free_slots(desks[0], provider_id, start, end)
        if slots:
            new_id = await client.reschedule(desks[0], appointment_id, slots[-1]["id"])
            if await client.wait(desks[0], new_id) == "confirmed":
                summary.rescheduled = 1

    for n in range(max(0, TARGET_WAITLIST - existing_waitlist)):
        provider = adult_providers[(n + 5) % len(adult_providers)]
        await client.join_waitlist(
            desks[1 % len(desks)], net.patient_ids[walk_ins[-(n + 1)]],
            net.provider_ids[provider.licence], start, now + timedelta(days=7))
        summary.waitlisted += 1
    return summary
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker -k live`
Expected: `1 passed`

The reschedule waits until the *new* appointment confirms; the old one then becomes
`rescheduled` inside `RescheduleWorkflow`. If the assertion on `RESCHEDULED` races, poll the old
appointment (via `client.wait`-style loop on `GET /appointments/{old}` until it is not
`confirmed`) before asserting — do not add a sleep.

- [ ] **Step 5: Commit**

```bash
git add app/demo/live.py tests/tiers/t3_integration/test_demo_data.py
git commit -m "feat(demo): live activity through the real API -- bookings, a cancel, a reschedule, the waitlist"
```

---

### Task 7: Orchestration and the safety guard

**Files:**
- Modify: `app/demo/__init__.py`
- Modify: `tests/tiers/t3_integration/test_demo_data.py` (append guard test)

- [ ] **Step 1: Append the failing test**

```python
# --- appended to tests/tiers/t3_integration/test_demo_data.py ------------------------
from app.demo import DemoRefused, demo_seed  # move to the top imports
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit


async def test_demo_seed_refuses_a_database_with_real_patients(demo_factory, db_settings):
    async with demo_factory() as session:
        session.add(Patient(mrn=f"REAL-{uuid.uuid4().hex[:8]}", first_name="Real",
                            last_name="Person"))
        await session.commit()
    try:
        with pytest.raises(DemoRefused):
            await demo_seed(settings=db_settings, password=PASSWORD,
                            api_url="http://unused", audit=RecordingAudit())
    finally:
        async with demo_factory() as session:
            await session.execute(text("DELETE FROM patients WHERE mrn LIKE 'REAL-%'"))
            await session.commit()
```

Add `import uuid` and `from sqlalchemy import text` to the test imports.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker -k refuses`
Expected: FAIL with `ImportError: cannot import name 'DemoRefused'`

- [ ] **Step 3: Write the orchestration in `app/demo/__init__.py`**

```python
# app/demo/__init__.py
"""Demo data for a deployed SmartHealth: an imported history plus live activity.

`demo_seed` builds the network, imports six months of history up to yesterday, opens two
weeks of slots and books live activity through the API. `demo_refresh` brings an existing
demo up to date: history from the last imported day to yesterday, slots two weeks ahead,
live bookings topped up. Both are idempotent and commit one history day at a time.

Spec: docs/superpowers/specs/2026-11-16-demo-seed-data-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditEvent
from app.core.clock import Clock
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.demo.history import (
    ACTOR,
    HISTORY_DAYS,
    ORIGIN,
    DayCounts,
    day_imported,
    import_day,
    last_imported_day,
    plan_day,
)
from app.demo.live import LiveSummary, run_live
from app.demo.network import (
    DEFAULT_TIMEZONE,
    MRN_PREFIX,
    UPCOMING_DAYS,
    Network,
    build_network,
    ensure_slots,
)
from app.modules.clinical.storage import FilesystemDocumentStore
from app.modules.patients.models import Patient
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    ProviderSlot,
    WaitlistEntry,
    WaitlistStatus,
)
from app.settings import Settings


class DemoRefused(Exception):
    """The database holds data that is not the demo's; refusing to mix fiction into it."""


@dataclass
class DemoSummary:
    created: dict[str, int] = field(default_factory=dict)
    slots: int = 0
    days_imported: int = 0
    history: DayCounts = field(default_factory=DayCounts)
    live: LiveSummary | None = None


async def foreign_patients(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Patient).where(
        Patient.mrn.not_like(f"{MRN_PREFIX}%")))


async def demo_seed(*, settings: Settings, password: str, api_url: str, audit,
                    timezone: str = DEFAULT_TIMEZONE, http: httpx.AsyncClient | None = None,
                    clock: Clock | None = None) -> DemoSummary:
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            others = await foreign_patients(session)
            if others:
                raise DemoRefused(
                    f"{others} patient(s) here are not demo patients (MRN not {MRN_PREFIX}…); "
                    "demo-seed only runs on a database it owns")
            net, created = await build_network(session, password=password, timezone=timezone)
            await session.commit()
        return await _top_up(factory, net, created, settings=settings, password=password,
                             api_url=api_url, audit=audit, http=http, clock=clock)
    finally:
        await engine.dispose()


async def demo_refresh(*, settings: Settings, password: str, api_url: str, audit,
                       http: httpx.AsyncClient | None = None,
                       clock: Clock | None = None) -> DemoSummary:
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            net, created = await build_network(session, password=password)
            await session.commit()
        return await _top_up(factory, net, created, settings=settings, password=password,
                             api_url=api_url, audit=audit, http=http, clock=clock)
    finally:
        await engine.dispose()


async def _top_up(factory: async_sessionmaker[AsyncSession], net: Network,
                  created: dict[str, int], *, settings: Settings, password: str, api_url: str,
                  audit, http: httpx.AsyncClient | None, clock: Clock | None) -> DemoSummary:
    now = (clock or Clock()).now()
    today = now.astimezone(net.tz).date()
    summary = DemoSummary(created=created)

    async with factory() as session:
        summary.slots = await ensure_slots(session, net, first_day=today + timedelta(days=1),
                                           last_day=today + timedelta(days=UPCOMING_DAYS))
        await session.commit()

    async with factory() as session:
        last = await last_imported_day(session, net)
    first = (last + timedelta(days=1)) if last else today - timedelta(days=HISTORY_DAYS)
    store = FilesystemDocumentStore.from_settings(settings)
    imported: list[date] = []
    day = first
    while day < today:
        async with factory() as session:
            if not await day_imported(session, net, day):
                counts = await import_day(session, net, plan_day(day, tz=net.tz, today=today),
                                          store=store, settings=settings, now=now)
                await session.commit()
                summary.history.add(counts)
                if counts.slots:
                    imported.append(day)
        day += timedelta(days=1)
    summary.days_imported = len(imported)
    if imported:
        await audit.record(AuditEvent(
            entity_type="demo_history", entity_id=f"{imported[0]}..{imported[-1]}",
            action="imported", actor_user_id=ACTOR, after=summary.history.__dict__))

    async with factory() as session:
        upcoming, waitlist = await _live_state(session, net, now)
    owned = http is None
    client = http or httpx.AsyncClient(base_url=api_url, timeout=30)
    try:
        summary.live = await run_live(client, net, password=password, now=now,
                                      existing_upcoming=upcoming, existing_waitlist=waitlist)
    finally:
        if owned:
            await client.aclose()
    return summary


async def _live_state(session: AsyncSession, net: Network, now: datetime) -> tuple[int, int]:
    providers = list(net.provider_ids.values())
    upcoming = await session.scalar(
        select(func.count()).select_from(Appointment)
        .join(ProviderSlot, ProviderSlot.id == Appointment.slot_id)
        .where(Appointment.provider_id.in_(providers), Appointment.origin != ORIGIN,
               Appointment.status.in_((AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED)),
               ProviderSlot.starts_at > now.astimezone(UTC)))
    waitlist = await session.scalar(
        select(func.count()).select_from(WaitlistEntry)
        .where(WaitlistEntry.patient_id.in_(net.patient_ids),
               WaitlistEntry.status == WaitlistStatus.ACTIVE))
    return upcoming, waitlist


__all__ = ["DemoRefused", "DemoSummary", "demo_refresh", "demo_seed"]
```

(`WaitlistEntry` and `WaitlistStatus` are the real names, `app/modules/scheduling/models.py:255`.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/tiers/t3_integration/test_demo_data.py -q -m docker`
Expected: all demo tests pass (`7 passed`).

- [ ] **Step 5: Commit**

```bash
git add app/demo/__init__.py tests/tiers/t3_integration/test_demo_data.py
git commit -m "feat(demo): demo_seed and demo_refresh -- guarded, idempotent, one committed day at a time"
```

---

### Task 8: CLI commands

**Files:**
- Modify: `app/cli.py` (imports, a runner, two subparsers, two branches in `main`)
- Test: `tests/tiers/t0_unit/test_demo_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/tiers/t0_unit/test_demo_cli.py
"""The demo commands refuse to run unless asked to with --allow-demo-data."""

from __future__ import annotations

import pytest

from app import cli

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("command", ["demo-seed", "demo-refresh"])
def test_without_allow_demo_data_nothing_runs(command, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "run_demo", lambda **kwargs: ran.append(kwargs))
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--password", "x"])
    assert exit_info.value.code == 1 and not ran
    assert "--allow-demo-data" in capsys.readouterr().err


def test_demo_seed_parses_its_options():
    args = cli._parse_args(["demo-seed", "--password", "p", "--allow-demo-data",
                            "--api-url", "http://api:8000", "--timezone", "Asia/Karachi"])
    assert (args.password, args.api_url, args.timezone, args.allow_demo_data) == (
        "p", "http://api:8000", "Asia/Karachi", True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_cli.py -q`
Expected: FAIL (`argparse` error: invalid choice `demo-seed`)

- [ ] **Step 3: Wire the commands into `app/cli.py`**

Add to the imports (after the `app.seed` imports):

```python
from app.demo import DemoRefused, demo_refresh, demo_seed
from app.demo.network import DEFAULT_TIMEZONE
```

Add this function after `run_seed`:

```python
async def run_demo(*, settings: Settings, command: str, password: str, api_url: str,
                   timezone: str) -> None:
    """`demo-seed` or `demo-refresh`, reporting what changed. Mongo is opened here because
    the import writes one audit entry per run."""
    mongo = create_mongo_client(settings)
    try:
        audit = AuditLog(get_audit_collection(mongo, settings), Clock())
        if command == "demo-seed":
            summary = await demo_seed(settings=settings, password=password, api_url=api_url,
                                      audit=audit, timezone=timezone)
        else:
            summary = await demo_refresh(settings=settings, password=password,
                                         api_url=api_url, audit=audit)
    except DemoRefused as refused:
        raise _fail(str(refused)) from refused
    finally:
        await mongo.close()
    print(f"network created: {summary.created}")
    print(f"upcoming slots added: {summary.slots}")
    print(f"history days imported: {summary.days_imported} -> {summary.history.__dict__}")
    print(f"live activity: {summary.live.__dict__ if summary.live else 'none'}")
```

Add the subparsers inside `_parse_args`, before `return parser.parse_args(argv)`:

```python
    for name, help_text in (
        ("demo-seed", "Build the demo network, import 6 months of history, book live activity."),
        ("demo-refresh", "Bring the demo up to date: history to yesterday, slots, live activity."),
    ):
        demo_parser = subparsers.add_parser(name, help=help_text)
        demo_parser.add_argument("--password", required=True,
                                 help="Password for every demo login. Required; no default.")
        demo_parser.add_argument("--api-url", default="http://localhost:8000",
                                 help="Part A's base URL, for the live bookings.")
        demo_parser.add_argument("--allow-demo-data", action="store_true",
                                 help="Required: confirms this database is for demo data.")
        if name == "demo-seed":
            demo_parser.add_argument("--timezone", default=DEFAULT_TIMEZONE,
                                     help="IANA timezone of the demo clinics.")
```

Add the branch at the end of `main`:

```python
    elif args.command in ("demo-seed", "demo-refresh"):
        if not args.allow_demo_data:
            print("error: refusing to write demo data without --allow-demo-data",
                  file=sys.stderr)
            raise SystemExit(1)
        asyncio.run(run_demo(
            settings=Settings(), command=args.command, password=args.password,
            api_url=args.api_url, timezone=getattr(args, "timezone", DEFAULT_TIMEZONE)))
```

> The unit test monkeypatches `run_demo`, so `asyncio.run(run_demo(...))` is never reached when
> the flag is missing. With the flag, `asyncio.run` would receive the lambda's `None`; the test
> does not take that path. Check `_fail` exists in `app/cli.py` (it does, line ~111) and
> returns a `SystemExit`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/tiers/t0_unit/test_demo_cli.py tests/meta -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/cli.py tests/tiers/t0_unit/test_demo_cli.py
git commit -m "feat(cli): demo-seed and demo-refresh, refusing without --allow-demo-data"
```

---

### Task 9: Test case YAML (core-path Stop hook)

**Files:**
- Create: `tests/cases/ops-005-demo-history-is-consistent-and-idempotent.yaml`

- [ ] **Step 1: Write the case**

```yaml
id: ops-005-demo-history-is-consistent-and-idempotent
title: "Imported demo history holds every database rule, is marked legacy_import, re-runs add nothing, and the analytics reconcile finds no drift"
requirement: [PART-A-FR-5]
tier: integration
priority: P2
status: ready
# The demo data spec (docs/superpowers/specs/2026-11-16-demo-seed-data-design.md): history
# cannot come from the workflows (past slots are unbookable), so it is imported -- and the
# reconcile reporting zero drift is the proof that the imported rollup and the imported rows
# agree. The other demo properties (guard, live phase, idempotency) are in the same file.
impl: "tests/tiers/t3_integration/test_demo_data.py::test_the_nightly_reconcile_finds_no_drift_in_imported_days"
```

- [ ] **Step 2: Validate the catalog**

Run: `python -m tests.runner.route_check`
Expected: exits 0; the new case is listed under integration.

- [ ] **Step 3: Commit**

```bash
git add tests/cases/ops-005-demo-history-is-consistent-and-idempotent.yaml
git commit -m "test(cases): ops-004 -- imported demo history is consistent and idempotent"
```

---

### Task 10: Documentation and the full check

**Files:**
- Modify: `README.md` (a "Demo data" subsection under "Run it")
- Modify: `docs/index.html` (two rows in the "Useful commands" table of `#run`)
- Modify: `CLAUDE.md` (two rows in the commands table)
- Modify: `docs/PRD.md` (record the no-show billing finding as a new risk at the end of §9)

- [ ] **Step 1: README** — under the existing seed instructions in "Run it", add:

```markdown
### Demo data (for a deployed review environment)

`seed` gives a small, empty network for development. For a demo that looks lived-in:

    python -m app.cli demo-seed --password '<choose one>' --api-url http://localhost:8000 --allow-demo-data
    python -m app.cli demo-refresh --password '<same>' --api-url http://localhost:8000 --allow-demo-data

`demo-seed` builds 3 clinics, 15 providers across all 10 documented specialties and 800
patients; imports six months of history up to yesterday (visits, signed notes, prescriptions,
test results, cancellations, no-shows, billing and analytics), marked
`appointments.origin = 'legacy_import'`; opens two weeks of slots; and books live activity
through the API, so Temporal, Kafka, Celery and Mailpit all have something to show. Run
`demo-refresh` before a review to bring history up to yesterday and top up live bookings.
Both refuse without `--allow-demo-data`, and `demo-seed` refuses a database holding non-demo
patients. Logins (all with your password): `demo.admin@medinova.example`,
`demo.desk1@medinova.example`, every provider as `first.last@medinova.example`
(e.g. `amara.osei@medinova.example`), and five patients (`ava.smith2@example.com` style — run
`demo-seed` to see the list it prints). Design: `docs/superpowers/specs/2026-11-16-demo-seed-data-design.md`.
```

Before writing the patient login example, compute the real address:
`python -c "from app.demo.network import patient_identity as p; print([p(i).email for i in (1,2,3,4,5)])"`
and list those five addresses instead of the placeholder sentence.

- [ ] **Step 2: Site** — in `docs/index.html`, in the `#run` section's "Useful commands" table, add after the `link-patient` row:

```html
    <tr><td><code>python -m app.cli demo-seed --password … --api-url … --allow-demo-data</code></td><td>Demo network, six months of imported history (<code>origin = 'legacy_import'</code>), live bookings through the API</td></tr>
    <tr><td><code>python -m app.cli demo-refresh --password … --api-url … --allow-demo-data</code></td><td>Bring the demo up to yesterday and top up live activity</td></tr>
```

- [ ] **Step 3: CLAUDE.md** — in the commands table, after the `seed` row, add:

```markdown
| `python -m app.cli demo-seed --password … --api-url … --allow-demo-data [--timezone …]` / `demo-refresh` | demo network + six months of imported history (`origin = 'legacy_import'`, no events, analytics written to match) + live bookings through the API; refuses without the flag or on a database with non-demo patients (`app/demo/`) |
```

- [ ] **Step 4: PRD** — append a risk after the last `R-` row in §9 (use the next free id;
check with `grep -n "^| \*\*R-3" docs/PRD.md`):

```markdown
| **R-33** | A live no-show never releases its payment hold: `mark_no_show` emits `appointments.no_show` but nothing calls `billing.release`, so the billing record stays `authorized`; the patient policy is silent on no-shows. Found while designing the demo data (spec §10), which imports no-shows as `voided` | **Open** — decide void vs charge, then release (or capture) in the no-show path |
```

Match the PRD table's existing column count and formatting exactly (read two neighbouring
rows first).

- [ ] **Step 5: Full check**

Run: `python scripts/gen_docs_data.py --check` → `checked: …` (the CLI is not in the generated data; nothing should be stale).
Run: `python -m ruff check app tests services` → `All checks passed!` (the plan's snippets were
written to the 100-character limit; wrap any line ruff still flags, without changing behaviour).
Run: `python -m pytest -m "not docker" -q` → all pass (1375 + the new unit tests).
Run: `python -m pytest -m docker -q tests/tiers/t3_integration/test_demo_data.py tests/tiers/t3_integration/test_seed.py` → all pass.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/index.html CLAUDE.md docs/PRD.md
git commit -m "docs: demo-seed and demo-refresh, and the no-show billing gap (R-33)"
```

---

## Self-review (done while writing)

- **Spec coverage:** commands and flags (Task 8), idempotency (Tasks 3, 5), determinism (Task 4),
  guard (Task 7), `app/demo/` layout (all), network §5 (Tasks 2–3), history §6 incl. analytics
  and audit entry (Tasks 5, 7), live §7 (Task 6), testing §8 (unit tests in Tasks 1, 2, 4, 8;
  integration in 3, 5, 6, 7), case YAML and docs (Tasks 9–10), finding §10 (Task 10, R-33).
- **Placeholder scan:** no open "check first" steps remain (the `authenticate` signature and the
  `WaitlistEntry` name were verified against the code while writing); the README patient-login
  line is computed in Step 1 of Task 10, not guessed.
- **Fixed during review:** `purge_demo` first deleted every `@example.com` user, which would have
  removed other tests' logins; it now deletes exactly the demo's addresses.
- **Names:** `build_network`, `ensure_slots`, `plan_day`, `import_day`, `day_imported`,
  `last_imported_day`, `run_live`, `demo_seed`, `demo_refresh`, `DemoRefused`, `Network`
  (`provider_ids`, `clinic_ids`, `department_ids`, `provider_user_ids`, `patient_ids`, `tz`) are
  used consistently across tasks.
