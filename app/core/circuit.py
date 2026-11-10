"""A circuit breaker for calls to a dependency that can be down (the payment gateway).

Without one, a gateway outage turns every booking into a slow failure: each billing
activity waits out its HTTP timeout, Temporal retries it, and the worker's activity slots
fill with calls that cannot succeed. With one, after `threshold` consecutive failures the
circuit *opens* and calls fail immediately with `CircuitOpen` for `cooldown`; then one
trial call is let through (*half-open*) and its outcome closes or re-opens the circuit.

Per process, in memory: each worker learns for itself, which is enough -- the point is to
stop one process hammering a dead dependency, not to coordinate a fleet.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.errors import ServiceUnavailable


class CircuitOpen(ServiceUnavailable):
    """The dependency failed too often recently; not calling it. Retryable later."""


@dataclass
class CircuitBreaker:
    name: str
    threshold: int = 5
    cooldown_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self.clock() - self._opened_at >= self.cooldown_seconds:
            return "half-open"
        return "open"

    def before_call(self) -> None:
        """Raise `CircuitOpen` if the call must not be made."""
        if self.state == "open":
            raise CircuitOpen(f"{self.name} is failing; not calling it for a while")

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        if self.state == "half-open":
            self._opened_at = self.clock()  # the trial failed: open again for a cooldown
            return
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = self.clock()
