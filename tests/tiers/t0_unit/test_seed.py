"""Seed-data logic, without a database.

The generation rules are pure functions, so the properties that matter -- slots land
in the future, on weekdays, and never overlap -- are provable here in milliseconds
rather than against a live stack. The one exception is idempotency, which is a
statement about the database and belongs in T3.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from app import seed
from app.core.clock import FixedClock

pytestmark = pytest.mark.unit

#: A Wednesday, so the window straddles a weekend without starting on one.
WEDNESDAY = datetime(2026, 9, 16, 14, 30, tzinfo=UTC)


def test_every_generated_slot_is_in_the_future():
    """A seeded slot in the past is worthless: Week 2's booking activity must reject
    it (risk R-3), so a demo would fail on data we created ourselves."""
    starts = seed._slot_starts(WEDNESDAY)
    assert starts, "expected a non-empty window"
    assert all(start > WEDNESDAY for start in starts)


def test_no_slot_falls_on_a_weekend():
    starts = seed._slot_starts(WEDNESDAY)
    assert {start.weekday() for start in starts}.isdisjoint({5, 6})


def test_slots_never_overlap():
    """The GiST exclusion constraint forbids two overlapping slots for one provider,
    and every provider gets this same list. A generator that produced an overlap would
    fail at insert -- correctly, but only after someone ran it against a database."""
    starts = sorted(seed._slot_starts(WEDNESDAY))
    for earlier, later in itertools.pairwise(starts):
        assert earlier + seed.SLOT_DURATION <= later


def test_slot_generation_is_deterministic_for_a_fixed_clock():
    """Idempotency depends on this: a rerun must compute the same starts, or it would
    insert a second overlapping set and trip the exclusion constraint."""
    assert seed._slot_starts(WEDNESDAY) == seed._slot_starts(WEDNESDAY)


def test_the_window_covers_the_configured_number_of_days():
    starts = seed._slot_starts(WEDNESDAY)
    span = max(starts).date() - min(starts).date()
    assert span < timedelta(days=seed.SLOT_WINDOW_DAYS)


def test_patient_identities_are_stable_across_calls():
    """MRN is the natural key the seeder matches on. If it moved between runs, a
    rerun would create duplicates instead of finding the existing rows."""
    assert [seed._patient_identity(i) for i in range(20)] == [
        seed._patient_identity(i) for i in range(20)
    ]


def test_mrns_are_unique_and_match_the_public_helper():
    mrns = seed.seeded_patient_mrns(500)
    assert len(set(mrns)) == 500
    assert mrns[0] == seed.LINKED_PATIENT_MRN


def test_summary_reports_every_entity_kind():
    lines = "\n".join(seed.SeedSummary(clinics=2, slots=240).as_lines())
    for entity in ("clinics", "departments", "providers", "patients", "users", "slots"):
        assert entity in lines
    assert "240" in lines


def test_a_fixed_clock_drives_slot_generation():
    """Harness requirement 6: time comes from an injectable provider. `seed()` takes a
    Clock so a test can pin the window rather than depending on the day it runs."""
    clock = FixedClock(WEDNESDAY)
    assert seed._slot_starts(clock.now()) == seed._slot_starts(WEDNESDAY)


def test_the_seeder_never_touches_week_two_state():
    """The design rule, enforced mechanically rather than by comment.

    Appointments, visits and waitlist entries are *workflow outputs*: an appointment
    reaches `confirmed` only after slot reservation, billing pre-check and notification
    scheduling all succeed. Seeding one fabricates state no workflow produced, and
    Week 2's tests would then validate against fiction.

    Asserted by reading the module's imports rather than its behaviour, so the rule
    holds even for a code path no test happens to execute.
    """
    source = pathlib.Path(seed.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = {"Appointment", "Visit", "WaitlistEntry"}
    leaked = {name for name in imported if name.rsplit(".", 1)[-1] in forbidden}
    assert not leaked, (
        f"app/seed.py imports Week 2 scheduling state: {sorted(leaked)}. "
        "Appointments, visits and waitlist entries are workflow outputs and must not "
        "be seeded -- see the module docstring."
    )
