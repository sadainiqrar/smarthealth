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
