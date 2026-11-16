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
