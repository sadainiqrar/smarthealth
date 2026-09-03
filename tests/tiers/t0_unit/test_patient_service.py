"""Unit coverage for `app.modules.patients.service` that needs no database.

`list_patients`'s search escaping (Fix 1 -- `Column.contains(search, autoescape=True)`
instead of a hand-built `f"%{search}%"` passed to `ilike`) is only observable in the
compiled SQL: whether "a_b" over-matches "axb" is a property of how Postgres evaluates
LIKE, not of anything Python can assert without a live connection. That over-match /
fix comparison was proven separately against the running test-stack database. What a
unit test *can* assert without a database is that the statement `list_patients` sends
downstream carries an ESCAPE clause and an escaped `_`/`%` -- i.e. that escaping is
actually wired in, not just present in some other expression this test doesn't exercise.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.pagination import PageParams
from app.modules.patients.service import list_patients

pytestmark = pytest.mark.unit


class _CapturingSession:
    """A fake `AsyncSession` that records every statement handed to it instead of
    talking to a database, so the real `list_patients` code path can be exercised and
    its generated SQL inspected."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalar(self, stmt: Any) -> int:
        self.statements.append(stmt)
        return 0

    async def scalars(self, stmt: Any) -> list[Any]:
        self.statements.append(stmt)
        return []


def _compiled(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def test_search_escapes_like_metacharacters_in_the_compiled_statement():
    session = _CapturingSession()

    await list_patients(session, page=PageParams(limit=50, offset=0), search="a_b")

    # Both the count statement and the row statement were sent through this session.
    assert len(session.statements) == 2
    for stmt in session.statements:
        compiled = _compiled(stmt)
        assert "ESCAPE" in compiled
        # The literal underscore in the search term is escaped so it cannot act as a
        # LIKE single-character wildcard; only the wildcards `contains()` itself adds
        # (the leading/trailing `%`) are unescaped.
        assert "a/_b" in compiled or "a\\_b" in compiled


async def test_search_still_wraps_the_term_for_an_ordinary_substring_match():
    session = _CapturingSession()

    await list_patients(session, page=PageParams(limit=50, offset=0), search="Blog")

    compiled = _compiled(session.statements[0])
    # The generic postgresql dialect doubles a literal `%` (paramstyle escaping), so the
    # wrapped term reads `'%%' || 'Blog' || '%%'` here; the asyncpg dialect actually
    # used at runtime sends a single `%` -- either way, the term itself is untouched
    # and simply wrapped in wildcards, which is what an "ordinary search still works"
    # check needs to prove.
    assert "|| 'Blog' ||" in compiled
    assert "ESCAPE" in compiled
