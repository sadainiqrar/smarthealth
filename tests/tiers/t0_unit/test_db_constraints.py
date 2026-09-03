"""Unit coverage for `app.db.constraints.violated_constraint`.

The helper's whole reason to exist is that the obvious implementation --
`getattr(exc.orig, "constraint_name", None)` -- always returns None against asyncpg.
`exc.orig` is SQLAlchemy's asyncpg dialect shim, which carries only
`sqlstate`/`pgcode`; the real `asyncpg.exceptions.UniqueViolationError` holding
`constraint_name` hangs off it as `__cause__`. That was established empirically
against the running database. These fakes pin the shape so a future "simplification"
back to inspecting `exc.orig` alone fails here instead of turning a 409 into a 500.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.constraints import violated_constraint

pytestmark = pytest.mark.unit


class _AsyncpgShim(Exception):
    """Stands in for SQLAlchemy's asyncpg dialect error: no `constraint_name`."""

    sqlstate = "23505"
    pgcode = "23505"


class _RealAsyncpgError(Exception):
    """Stands in for `asyncpg.exceptions.UniqueViolationError`."""

    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


def _integrity_error(orig: Exception) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, orig)


def test_reads_the_constraint_name_off_the_cause():
    shim = _AsyncpgShim()
    shim.__cause__ = _RealAsyncpgError("uq_providers_license_number")

    assert violated_constraint(_integrity_error(shim)) == "uq_providers_license_number"


def test_falls_back_to_orig_when_it_carries_the_name_itself():
    """A different driver may put the name on `orig` directly; do not lose that."""
    assert violated_constraint(_integrity_error(_RealAsyncpgError("ix_patients_mrn"))) == (
        "ix_patients_mrn"
    )


def test_returns_none_when_no_name_is_available():
    """A shim with no cause is exactly the asyncpg shape minus the wrapped error;
    the caller must then re-raise rather than mistranslate an unrelated violation."""
    assert violated_constraint(_integrity_error(_AsyncpgShim())) is None


def test_returns_none_when_orig_is_absent():
    error = IntegrityError("INSERT ...", {}, None)  # type: ignore[arg-type]
    assert violated_constraint(error) is None
