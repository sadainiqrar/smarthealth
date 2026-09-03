"""Unit coverage for `app.modules.providers.service` that needs no database.

Mirrors `test_patient_service.py`. The properties pinned here -- that the search
escapes LIKE metacharacters, stays case-insensitive, and that the `specialty` filter
is an equality test rather than a pattern match -- are only observable in the SQL the
service emits. Their end-to-end behaviour was proven separately against the running
database; what these tests add is a regression gate, so a future "simplification" of
`icontains` back to `contains`, or of the specialty equality back to an `ilike`,
fails loudly here instead of shipping.

Compiled against `sqlalchemy.dialects.postgresql.dialect()` -- the dialect asyncpg
uses at runtime -- `icontains(..., autoescape=True)` compiles to a native
`ILIKE ... ESCAPE`, not the `lower(col) LIKE lower(term)` form the generic dialect
produces. `contains()` compiles to a plain, case-sensitive `LIKE` under the same
dialect, so `ILIKE` is what distinguishes the two here.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.pagination import PageParams
from app.modules.providers.service import list_providers

pytestmark = pytest.mark.unit


class _CapturingSession:
    """A fake `AsyncSession` recording every statement instead of executing it, so the
    real `list_providers` code path runs and its generated SQL can be inspected."""

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

    await list_providers(session, page=PageParams(limit=50, offset=0), search="a_b")

    assert len(session.statements) == 2
    for stmt in session.statements:
        compiled = _compiled(stmt)
        assert "ESCAPE" in compiled
        # The literal underscore is escaped so it cannot act as a LIKE
        # single-character wildcard; only the wildcards `icontains` itself adds (the
        # leading/trailing `%`) are left unescaped. SQLAlchemy's autoescape uses `/`
        # under this dialect; the backslash form is accepted in case that changes.
        assert "a/_b" in compiled or r"a\_b" in compiled


async def test_search_stays_case_insensitive_in_the_compiled_statement():
    """Pins `icontains` against a regression back to `contains`, which compiles to a
    case-sensitive `LIKE` and would stop "lovelace" matching "Lovelace"."""
    session = _CapturingSession()

    await list_providers(session, page=PageParams(limit=50, offset=0), search="lovelace")

    for stmt in session.statements:
        compiled = _compiled(stmt)
        assert "ILIKE" in compiled
        assert "ESCAPE" in compiled


async def test_search_still_wraps_the_term_for_an_ordinary_substring_match():
    session = _CapturingSession()

    await list_providers(session, page=PageParams(limit=50, offset=0), search="Love")

    compiled = _compiled(session.statements[0])
    assert "|| 'Love' ||" in compiled


async def test_the_specialty_filter_compares_for_equality_not_by_pattern():
    """A `%` passed as a specialty must be compared as a literal string. If this were
    an ILIKE, `specialty="%"` would match every provider in the system."""
    session = _CapturingSession()

    await list_providers(session, page=PageParams(limit=50, offset=0), specialty="%")

    for stmt in session.statements:
        # The generic postgresql dialect doubles a literal `%` for paramstyle escaping,
        # so the term reads `'%%'` here; the asyncpg dialect used at runtime sends a
        # single `%`. Either way it is an argument to `=`, never to a LIKE.
        compiled = _compiled(stmt).replace("%%", "%")
        assert "lower(providers.specialty) = '%'" in compiled
        assert "LIKE" not in compiled


async def test_the_count_and_the_page_share_the_same_conditions():
    """If the filter were applied to only one of the two, `total` would lie."""
    session = _CapturingSession()

    await list_providers(
        session, page=PageParams(limit=50, offset=0), specialty="Cardiology", search="Ada"
    )

    count_stmt, page_stmt = (_compiled(stmt) for stmt in session.statements)
    for compiled in (count_stmt, page_stmt):
        # The term is lowered in Python, so the literal reaching SQL is already
        # lowercase; `lower()` is applied to the column, which is what makes the
        # comparison case-insensitive without a pattern match.
        assert "lower(providers.specialty) = 'cardiology'" in compiled
        assert "ILIKE" in compiled


async def test_the_page_is_ordered_by_a_unique_tiebreaker():
    """Offset pagination over a non-unique sort key skips and repeats rows."""
    session = _CapturingSession()

    await list_providers(session, page=PageParams(limit=50, offset=0))

    page_stmt = _compiled(session.statements[1])
    assert "ORDER BY providers.created_at DESC, providers.id" in page_stmt
