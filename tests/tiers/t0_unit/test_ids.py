"""UUIDv7 generation, against RFC 9562 section 5.7.

The bit layout is hand-rolled (`uuid.uuid7()` is Python 3.14; this project targets
3.13), so these tests are the thing standing between a correct implementation and one
that produces plausible-looking values with the wrong version nibble.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.ids import uuid7
from app.db.all_models import Base

pytestmark = pytest.mark.unit

#: An arbitrary fixed instant, so ordering assertions do not depend on the wall clock.
FIXED_MS = 1_700_000_000_000


def test_version_nibble_is_seven():
    """RFC 9562 5.7: ver "set to 0b0111 (7)", bits 48-51."""
    assert uuid7().version == 7


def test_variant_is_rfc_4122():
    """RFC 9562 4.1: var "set to 0b10", bits 64-65."""
    assert uuid7().variant == uuid.RFC_4122


def test_the_leading_48_bits_are_the_millisecond_timestamp():
    """RFC 9562 5.7: unix_ts_ms is a 48-bit big-endian value occupying bits 0-47.

    Recovering it is what proves the field is where the spec says, rather than merely
    somewhere consistent.
    """
    assert (uuid7(now_ms=FIXED_MS).int >> 80) == FIXED_MS


def test_later_timestamps_sort_after_earlier_ones():
    """The whole point of v7 over v4: ids are time-ordered, so index inserts append
    near the right edge of the B-tree instead of scattering across it."""
    ids = [uuid7(now_ms=FIXED_MS + offset) for offset in (0, 1, 1000, 86_400_000)]
    assert ids == sorted(ids)


def test_ids_minted_in_the_same_millisecond_are_unique():
    """74 bits of randomness carry uniqueness within a millisecond. A collision here
    would be a duplicate primary key, so the margin is checked rather than assumed."""
    same_instant = {uuid7(now_ms=FIXED_MS) for _ in range(10_000)}
    assert len(same_instant) == 10_000


def test_now_ms_is_keyword_only():
    """Load-bearing, not style.

    SQLAlchemy decides how to call a column default by inspecting its signature: a
    callable it believes takes one positional argument receives the
    `ExecutionContext`. If `now_ms` were positional-capable and ever became required,
    a context object would arrive in its place and the first insert would raise.
    Keyword-only means there is nothing to guess about.
    """
    with pytest.raises(TypeError):
        uuid7(FIXED_MS)  # type: ignore[misc]


@pytest.mark.parametrize(
    "table_name",
    sorted(name for name, table in Base.metadata.tables.items() if "id" in table.c),
)
def test_every_table_generates_a_v7_primary_key(table_name):
    """Enumerates the metadata rather than listing tables, so a model added later is
    covered without anyone remembering to extend this test.

    Invokes the default the way SQLAlchemy does at flush time -- with an execution
    context -- because `Column.default.arg` is a *wrapper* around `uuid7`, not
    `uuid7` itself. Asserting `arg is uuid7` would fail while the behaviour is fine,
    and would miss the failure that actually matters: the wrapper forwarding its
    context argument into the generator.
    """
    default = Base.metadata.tables[table_name].c["id"].default
    assert default is not None and default.is_callable

    generated = default.arg(object())  # a stand-in for the ExecutionContext
    assert isinstance(generated, uuid.UUID)
    assert generated.version == 7
