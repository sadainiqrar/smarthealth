"""The assistant's state, in its own Redis: turn streams, cancel flags, revoked sessions.

**Turns are Redis Streams** (spec section 4.4). Every event of a turn is appended to
`turn:<id>` before it is sent, so a client that drops mid-answer reconnects with the same
turn id and `resume_from` = the last seq it saw, and the stream replays after it instead of
generating again. **The deny-list** holds, per user, the time their sessions were revoked
(from `identity.sessions_revoked`): a token issued before it is refused, though its
signature is fine -- this service never reads Part A's session store.

Redis and in-memory implementations behind one interface, as in Part A.
"""

from __future__ import annotations

import json
import time
from typing import Protocol

from redis.asyncio import Redis


class Stores(Protocol):
    async def append(self, turn_id: str, seq: int, event: dict) -> None: ...
    async def replay(self, turn_id: str, after: int) -> list[tuple[int, dict]]: ...
    async def exists(self, turn_id: str) -> bool: ...
    async def cancel(self, turn_id: str) -> bool: ...
    async def cancelled(self, turn_id: str) -> bool: ...
    async def revoke(self, user_id: str, revoked_at: float) -> None: ...
    async def revoked_at(self, user_id: str) -> float | None: ...


class RedisStores:
    def __init__(self, redis: Redis, *, turn_ttl: int) -> None:
        self._redis = redis
        self._ttl = turn_ttl

    async def append(self, turn_id: str, seq: int, event: dict) -> None:
        key = f"turn:{turn_id}"
        await self._redis.xadd(key, {"seq": seq, "event": json.dumps(event)})
        await self._redis.expire(key, self._ttl)

    async def replay(self, turn_id: str, after: int) -> list[tuple[int, dict]]:
        entries = await self._redis.xrange(f"turn:{turn_id}")
        out = [(int(fields["seq"]), json.loads(fields["event"])) for _, fields in entries]
        return [(seq, event) for seq, event in out if seq > after]

    async def exists(self, turn_id: str) -> bool:
        return bool(await self._redis.exists(f"turn:{turn_id}"))

    async def cancel(self, turn_id: str) -> bool:
        return bool(await self._redis.set(f"cancel:{turn_id}", "1", ex=self._ttl))

    async def cancelled(self, turn_id: str) -> bool:
        return bool(await self._redis.exists(f"cancel:{turn_id}"))

    async def revoke(self, user_id: str, revoked_at: float) -> None:
        # Keep the latest revocation; tokens live minutes, so a day is plenty.
        current = await self.revoked_at(user_id)
        if current is None or revoked_at > current:
            await self._redis.set(f"revoked:{user_id}", revoked_at, ex=86400)

    async def revoked_at(self, user_id: str) -> float | None:
        value = await self._redis.get(f"revoked:{user_id}")
        return float(value) if value is not None else None


class InMemoryStores:
    def __init__(self) -> None:
        self.turns: dict[str, list[tuple[int, dict]]] = {}
        self.cancels: set[str] = set()
        self.revocations: dict[str, float] = {}

    async def append(self, turn_id: str, seq: int, event: dict) -> None:
        self.turns.setdefault(turn_id, []).append((seq, event))

    async def replay(self, turn_id: str, after: int) -> list[tuple[int, dict]]:
        return [(s, e) for s, e in self.turns.get(turn_id, []) if s > after]

    async def exists(self, turn_id: str) -> bool:
        return turn_id in self.turns

    async def cancel(self, turn_id: str) -> bool:
        self.cancels.add(turn_id)
        return True

    async def cancelled(self, turn_id: str) -> bool:
        return turn_id in self.cancels

    async def revoke(self, user_id: str, revoked_at: float) -> None:
        self.revocations[user_id] = max(revoked_at, self.revocations.get(user_id, 0.0))

    async def revoked_at(self, user_id: str) -> float | None:
        return self.revocations.get(user_id)


def now() -> float:
    return time.time()
