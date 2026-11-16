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
