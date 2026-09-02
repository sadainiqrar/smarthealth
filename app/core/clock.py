"""Time source.

Injected rather than called directly so that time-dependent logic — slot windows,
token expiry, wait-time analytics — can be tested without patching the standard
library. Harness requirement 6: never `datetime.utcnow()` inline.
"""

from __future__ import annotations

from datetime import UTC, datetime


class Clock:
    """The real clock. Always timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock(Clock):
    """A clock frozen at one instant, for tests."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


_default_clock = Clock()


def get_clock() -> Clock:
    """FastAPI dependency. Override in tests with `app.dependency_overrides`."""
    return _default_clock
