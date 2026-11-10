"""Fixed-window counters, for login throttling and per-user API rate limits.

Redis in production (`RedisRateLimiter`), a dictionary in the contract tier
(`InMemoryRateLimiter`), one interface. Framework-free: the HTTP translation of a
refusal is `RateLimited`, a `DomainError` like every other.

Fixed windows, not sliding ones: a burst straddling a window boundary can reach twice
the limit for a moment. Accepted -- the limits exist to stop floods and guessing, which
a 2x edge does not help, and a fixed window is one `INCR` and one `EXPIRE`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from redis.asyncio import Redis

from app.core.errors import DomainError
from app.db.redis import namespaced_key
from app.settings import Settings


class RateLimited(DomainError):
    """Too many requests in the current window. Retrying after `Retry-After` may work."""

    status_code = 429

    def __init__(self, detail: str, *, retry_after: int) -> None:
        super().__init__(detail)
        self.headers = {"Retry-After": str(max(retry_after, 1))}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    count: int
    #: Seconds until the window resets.
    retry_after: int


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window: timedelta) -> Decision:
        """Count one event under `key`; allowed while the count is within `limit`."""

    async def reached(self, key: str, *, limit: int) -> bool:
        """Whether `key` has counted `limit` events or more, without counting one.

        "Five failures and you are locked out" is `reached(limit=5)`."""

    async def reset(self, key: str) -> None: ...


class RedisRateLimiter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    def _key(self, key: str) -> str:
        return namespaced_key(self._settings, f"rl:{key}")

    async def hit(self, key: str, *, limit: int, window: timedelta) -> Decision:
        name = self._key(key)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(name)
            # NX: only the first hit of a window sets the expiry, so the window is fixed
            # rather than pushed forward by every hit.
            pipe.expire(name, int(window.total_seconds()), nx=True)
            pipe.ttl(name)
            count, _, ttl = await pipe.execute()
        return Decision(allowed=count <= limit, count=int(count), retry_after=max(int(ttl), 1))

    async def reached(self, key: str, *, limit: int) -> bool:
        value = await self._redis.get(self._key(key))
        return value is not None and int(value) >= limit

    async def reset(self, key: str) -> None:
        await self._redis.delete(self._key(key))


class InMemoryRateLimiter:
    """The same semantics in a dictionary, for tests with no Redis."""

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}

    def _current(self, key: str) -> tuple[int, float] | None:
        entry = self._counts.get(key)
        if entry is not None and entry[1] <= time.monotonic():
            del self._counts[key]
            return None
        return entry

    async def hit(self, key: str, *, limit: int, window: timedelta) -> Decision:
        entry = self._current(key)
        count, expires = entry if entry else (0, time.monotonic() + window.total_seconds())
        count += 1
        self._counts[key] = (count, expires)
        retry_after = max(int(expires - time.monotonic()), 1)
        return Decision(allowed=count <= limit, count=count, retry_after=retry_after)

    async def reached(self, key: str, *, limit: int) -> bool:
        entry = self._current(key)
        return entry is not None and entry[0] >= limit

    async def reset(self, key: str) -> None:
        self._counts.pop(key, None)

    def clear(self) -> None:
        self._counts.clear()


async def enforce(
    limiter: RateLimiter, key: str, *, limit: int, window: timedelta, what: str
) -> None:
    """Count one request and raise `RateLimited` if it is over the limit."""
    decision = await limiter.hit(key, limit=limit, window=window)
    if not decision.allowed:
        raise RateLimited(
            f"too many {what}; try again in {decision.retry_after}s",
            retry_after=decision.retry_after,
        )
