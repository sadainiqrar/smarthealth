"""Collects `tests/cases/*.yaml` into pytest items.

Four buckets, four behaviours:
  - declarative  -> executed by the engine
  - unsupported  -> executed too, so the missing handler fails loudly rather than hiding
  - impl_backed  -> the pointer is verified here; the Python test runs on its own
  - blocked      -> skipped with the recorded reason, never reported as a pass
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.runner.discover import load_cases, route, unsupported_kinds
from tests.runner.engine import CaseContext, run_case
from tests.runner.schema import Case

REPO_ROOT = Path(__file__).resolve().parents[2]

_LOAD = load_cases()
_ROUTING = route(_LOAD.cases)


def _params(cases: list[Case]) -> list:
    return [
        pytest.param(case, id=case.id, marks=getattr(pytest.mark, case.tier))
        for case in cases
    ]


def test_catalog_has_no_validation_errors():
    """A malformed case must break the suite, not disappear from it."""
    assert not _LOAD.errors, "invalid case files:\n" + "\n".join(
        f"  {error}" for error in _LOAD.errors
    )


@pytest.mark.parametrize("case", _params(_ROUTING.declarative + _ROUTING.unsupported))
async def test_declarative_case(case: Case, api_client):
    missing = unsupported_kinds(case)
    if missing:
        pytest.fail(
            f"case '{case.id}' needs engine support for {sorted(missing)}, which does not "
            f"exist yet. Either implement the handler or set status: blocked with a reason."
        )
    await run_case(case, CaseContext(api=api_client))


@pytest.mark.parametrize("case", _params(_ROUTING.impl_backed))
def test_impl_pointer_resolves(case: Case):
    file_part, separator, function = case.impl.partition("::")
    assert separator, f"case '{case.id}': impl must be '<path>::<test function>'"
    target = REPO_ROOT / file_part
    assert target.is_file(), f"case '{case.id}': impl file {file_part} does not exist"
    source = target.read_text(encoding="utf-8")
    assert f"def {function}" in source, (
        f"case '{case.id}': {file_part} has no test function named '{function}'"
    )


@pytest.mark.parametrize("case", _params(_ROUTING.blocked))
def test_blocked_case_is_reported(case: Case):
    pytest.skip(f"blocked: {case.blocked_on}")
