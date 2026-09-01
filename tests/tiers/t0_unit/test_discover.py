import textwrap

import pytest

from tests.runner.discover import load_cases, route

pytestmark = pytest.mark.unit


def write(tmp_path, name: str, body: str):
    (tmp_path / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_loads_valid_cases_sorted_by_id(tmp_path):
    write(tmp_path, "sys-002-b.yaml", """
        id: sys-002-b
        title: B
        tier: contract
        steps: [{ api: { path: /health } }]
    """)
    write(tmp_path, "sys-001-a.yaml", """
        id: sys-001-a
        title: A
        tier: contract
        steps: [{ api: { path: /health } }]
    """)
    result = load_cases(tmp_path)
    assert result.ok
    assert [case.id for case in result.cases] == ["sys-001-a", "sys-002-b"]


def test_invalid_case_becomes_an_error_not_an_exception(tmp_path):
    write(tmp_path, "sys-003-bad.yaml", """
        id: sys-003-bad
        title: Asserts nothing
        tier: contract
    """)
    result = load_cases(tmp_path)
    assert not result.ok
    assert result.cases == []
    assert "anti-stub" in result.errors[0].message


def test_duplicate_ids_are_an_error(tmp_path):
    body = """
        id: sys-004-dup
        title: Duplicate
        tier: contract
        steps: [{ api: { path: /health } }]
    """
    write(tmp_path, "sys-004-dup.yaml", body)
    write(tmp_path, "sys-004-dup.yml", body)
    result = load_cases(tmp_path)
    assert not result.ok
    assert "duplicate id" in result.errors[0].message


def test_empty_directory_loads_cleanly(tmp_path):
    result = load_cases(tmp_path)
    assert result.ok
    assert result.cases == []


def test_route_buckets_cases_by_how_they_execute(tmp_path):
    write(tmp_path, "sys-010-declarative.yaml", """
        id: sys-010-declarative
        title: Declarative
        tier: contract
        steps: [{ api: { path: /health } }]
    """)
    write(tmp_path, "sys-011-impl.yaml", """
        id: sys-011-impl
        title: Impl backed
        tier: journey
        impl: tests/tiers/t4_journey/test_x.py::test_y
    """)
    write(tmp_path, "sys-012-blocked.yaml", """
        id: sys-012-blocked
        title: Blocked
        tier: journey
        status: blocked
        blocked_on: needs the chaos engine (P4)
    """)
    write(tmp_path, "sys-013-unsupported.yaml", """
        id: sys-013-unsupported
        title: Uses a step kind the engine cannot run yet
        tier: integration
        steps: [{ emit: { topic: appointments.booked } }]
    """)
    routing = route(load_cases(tmp_path).cases)
    assert [case.id for case in routing.declarative] == ["sys-010-declarative"]
    assert [case.id for case in routing.impl_backed] == ["sys-011-impl"]
    assert [case.id for case in routing.blocked] == ["sys-012-blocked"]
    assert [case.id for case in routing.unsupported] == ["sys-013-unsupported"]
