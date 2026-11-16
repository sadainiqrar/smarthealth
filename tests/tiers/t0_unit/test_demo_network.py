"""The demo network's shape, without a database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.demo.content import SPECIALTIES
from app.demo.network import (
    CLINICS,
    LINKED_PATIENT_INDEXES,
    MRN_PREFIX,
    PATIENT_COUNT,
    PROVIDERS,
    PROVIDERS_BY_LICENCE,
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
    for i in range(PATIENT_COUNT):
        if is_child(i):
            assert patient_identity(i).date_of_birth >= date(2012, 1, 1)
    for i in LINKED_PATIENT_INDEXES:
        assert not is_child(i)
        assert patient_identity(i).date_of_birth < date(2008, 1, 1)


@pytest.mark.parametrize(
    ("day", "first_start"),
    [
        # June: AEST, UTC+10. 09:00 local is 23:00 UTC the day before.
        (date(2026, 6, 1), datetime(2026, 5, 31, 23, 0, tzinfo=UTC)),
        # October: AEDT, UTC+11 after the DST change. 09:00 local is 22:00 UTC the day before.
        (date(2026, 10, 5), datetime(2026, 10, 4, 22, 0, tzinfo=UTC)),
        # April: AEST again after the DST change. 09:00 local is 23:00 UTC the day before.
        (date(2026, 4, 6), datetime(2026, 4, 5, 23, 0, tzinfo=UTC)),
    ],
)
def test_a_session_is_six_half_hours_from_nine_local_in_utc(day, first_start):
    starts = session_starts(day, ZoneInfo("Australia/Sydney"))
    assert len(starts) == 6 and all(s.tzinfo is UTC for s in starts)
    assert starts[0] == first_start
    assert all(b - a == timedelta(minutes=30) for a, b in zip(starts, starts[1:], strict=False))


def test_working_follows_the_weekly_schedule():
    osei = PROVIDERS_BY_LICENCE["DEMO-LIC-001"]  # Cardiology, weekdays (0, 2)
    assert working(osei, date(2026, 6, 1))  # Monday
    assert working(osei, date(2026, 6, 3))  # Wednesday
    assert not working(osei, date(2026, 6, 2))  # Tuesday
    assert not working(osei, date(2026, 6, 6))  # Saturday
