import pytest

from tests.runner.discover import route
from tests.runner.reports import render_catalog, render_traceability
from tests.runner.schema import Case

pytestmark = pytest.mark.unit

CASES = [
    Case.model_validate({
        "id": "apt-001-booking", "title": "Booking confirms after the workflow",
        "tier": "journey", "priority": "P0", "requirement": ["PART-A-FR-2"],
        "impl": "tests/tiers/t4_journey/test_booking.py::test_confirms",
    }),
    Case.model_validate({
        "id": "sys-001-health", "title": "Health responds", "tier": "contract",
        "priority": "P0", "requirement": ["PART-A-OBS-1"],
        "steps": [{"api": {"path": "/health", "expect": {"status": 200}}}],
    }),
    Case.model_validate({
        "id": "apt-002-waitlist", "title": "Waitlist promotion", "tier": "journey",
        "requirement": ["PART-A-FR-2"], "status": "blocked",
        "blocked_on": "needs the chaos engine (P4)",
    }),
]


def test_catalog_lists_every_case_with_its_metadata():
    markdown = render_catalog(route(CASES))
    assert "| apt-001-booking |" in markdown
    assert "| sys-001-health |" in markdown
    assert "P0" in markdown
    assert "needs the chaos engine (P4)" in markdown


def test_catalog_reports_counts_by_tier_and_priority():
    markdown = render_catalog(route(CASES))
    assert "Total cases | 3" in markdown
    assert "journey | 2" in markdown
    assert "P0 | 2" in markdown


def test_traceability_groups_cases_by_requirement():
    markdown = render_traceability(CASES)
    assert "PART-A-FR-2" in markdown
    lines = [line for line in markdown.splitlines() if line.startswith("| PART-A-FR-2")]
    assert len(lines) == 1
    assert "apt-001-booking" in lines[0]
    assert "apt-002-waitlist" in lines[0]


def test_traceability_flags_cases_with_no_requirement():
    orphan = Case.model_validate({
        "id": "sys-002-orphan", "title": "No requirement", "tier": "contract",
        "steps": [{"api": {"path": "/health", "expect": {"status": 200}}}],
    })
    markdown = render_traceability([*CASES, orphan])
    assert "sys-002-orphan" in markdown
    assert "(none)" in markdown
