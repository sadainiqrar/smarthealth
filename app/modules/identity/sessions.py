"""Server-side sessions and rotating refresh tokens (Weeks 3-5 spec D-25).

An access token is signed and short; the session it names is what makes it usable.
Deleting the session ends every token issued for it at once -- logout, "log out
everywhere", and deactivating a login all come down to that.

**One session is one refresh-token family.** A refresh token is `<session_id>.<secret>`.
The store keeps only the SHA-256 of the current secret and of the one before it. A
refresh presenting the current secret rotates it; presenting the *previous* one means a
token that was already used came back -- someone replayed a stolen copy -- and the whole
session is revoked, so the thief and the victim are both logged out and the victim's next
login is the only live session. Anything else is simply refused.

Redis in production (`RedisSessionStore`; rotation is one Lua script, so two concurrent
refreshes cannot both win), a dictionary in the contract tier (`InMemorySessionStore`).
"""

from __future__ import annotations

import enum
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from redis.asyncio import Redis

from app.db.redis import namespaced_key
from app.settings import Settings


@dataclass(frozen=True)
class IssuedSession:
    session_id: str
    refresh_token: str


class RotationOutcome(enum.Enum):
    ROTATED = "rotated"
    #: The previous secret came back: replay of a used token. The session is now gone.
    REUSED = "reused"
    #: No such session, or a secret that was never issued for it.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Rotation:
    outcome: RotationOutcome
    session_id: str | None = None
    user_id: str | None = None
    role: str | None = None
    refresh_token: str | None = None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def split_refresh_token(token: str) -> tuple[str, str] | None:
    session_id, sep, secret = token.partition(".")
    if not sep or not session_id or not secret:
        return None
    return session_id, secret


class SessionStore(Protocol):
    async def issue(self, *, user_id: str, role: str, lifetime: timedelta) -> IssuedSession: ...

    async def is_active(self, session_id: str) -> bool: ...

    async def rotate(self, refresh_token: str) -> Rotation: ...

    async def revoke(self, session_id: str) -> None: ...

    async def revoke_user(self, user_id: str) -> int:
        """End every session of `user_id`; returns how many there were."""


# Returns {outcome, user_id, role}. Runs atomically in Redis, which is the point: a
# check-then-set from Python would let two refreshes with the same secret both rotate.
_ROTATE = """
local key = KEYS[1]
if redis.call('EXISTS', key) == 0 then return {'unknown'} end
local current = redis.call('HGET', key, 'current')
local previous = redis.call('HGET', key, 'previous')
local user_id = redis.call('HGET', key, 'user_id')
if ARGV[1] == current then
  redis.call('HSET', key, 'previous', current, 'current', ARGV[2])
  return {'rotated', user_id, redis.call('HGET', key, 'role')}
end
if previous and ARGV[1] == previous then
  redis.call('DEL', key)
  return {'reused', user_id}
end
return {'unknown'}
"""


class RedisSessionStore:
    """`session:<id>` is a hash (user, role, current and previous secret digests) that
    expires with the session; `user-sessions:<user>` is the set of a user's sessions."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._rotate = redis.register_script(_ROTATE)

    def _session_key(self, session_id: str) -> str:
        return namespaced_key(self._settings, f"session:{session_id}")

    def _user_key(self, user_id: str) -> str:
        return namespaced_key(self._settings, f"user-sessions:{user_id}")

    async def issue(self, *, user_id: str, role: str, lifetime: timedelta) -> IssuedSession:
        session_id = uuid.uuid4().hex
        secret = _new_secret()
        seconds = int(lifetime.total_seconds())
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self._session_key(session_id),
                mapping={"user_id": user_id, "role": role, "current": _digest(secret)},
            )
            pipe.expire(self._session_key(session_id), seconds)
            pipe.sadd(self._user_key(user_id), session_id)
            # The index outlives every session in it; entries whose session expired are
            # harmless, revoke_user deletes keys that are already gone.
            pipe.expire(self._user_key(user_id), seconds)
            await pipe.execute()
        return IssuedSession(session_id, f"{session_id}.{secret}")

    async def is_active(self, session_id: str) -> bool:
        return bool(await self._redis.exists(self._session_key(session_id)))

    async def rotate(self, refresh_token: str) -> Rotation:
        parts = split_refresh_token(refresh_token)
        if parts is None:
            return Rotation(RotationOutcome.UNKNOWN)
        session_id, secret = parts
        new_secret = _new_secret()
        result = await self._rotate(
            keys=[self._session_key(session_id)], args=[_digest(secret), _digest(new_secret)]
        )
        outcome = RotationOutcome(result[0])
        if outcome is RotationOutcome.ROTATED:
            return Rotation(
                outcome, session_id=session_id, user_id=result[1], role=result[2],
                refresh_token=f"{session_id}.{new_secret}",
            )
        if outcome is RotationOutcome.REUSED:
            await self._redis.srem(self._user_key(result[1]), session_id)
            return Rotation(outcome, session_id=session_id, user_id=result[1])
        return Rotation(outcome)

    async def revoke(self, session_id: str) -> None:
        user_id = await self._redis.hget(self._session_key(session_id), "user_id")
        await self._redis.delete(self._session_key(session_id))
        if user_id:
            await self._redis.srem(self._user_key(user_id), session_id)

    async def revoke_user(self, user_id: str) -> int:
        session_ids = await self._redis.smembers(self._user_key(user_id))
        keys = [self._session_key(sid) for sid in session_ids]
        removed = await self._redis.delete(*keys) if keys else 0
        await self._redis.delete(self._user_key(user_id))
        return int(removed)


@dataclass
class _Entry:
    user_id: str
    role: str
    current: str
    previous: str | None
    expires: float


class InMemorySessionStore:
    """The same semantics in a dictionary, for tests with no Redis."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Entry] = {}

    def _live(self, session_id: str) -> _Entry | None:
        entry = self._sessions.get(session_id)
        if entry is not None and entry.expires <= time.monotonic():
            del self._sessions[session_id]
            return None
        return entry

    async def issue(self, *, user_id: str, role: str, lifetime: timedelta) -> IssuedSession:
        return self.issue_now(user_id=user_id, role=role, lifetime=lifetime)

    def issue_now(
        self, *, user_id: str, role: str, lifetime: timedelta = timedelta(hours=1)
    ) -> IssuedSession:
        """`issue`, synchronously -- an in-memory store needs no I/O, and test helpers
        that build headers outside an event loop use this."""
        session_id = uuid.uuid4().hex
        secret = _new_secret()
        self._sessions[session_id] = _Entry(
            user_id, role, _digest(secret), None, time.monotonic() + lifetime.total_seconds()
        )
        return IssuedSession(session_id, f"{session_id}.{secret}")

    async def is_active(self, session_id: str) -> bool:
        return self._live(session_id) is not None

    async def rotate(self, refresh_token: str) -> Rotation:
        parts = split_refresh_token(refresh_token)
        if parts is None:
            return Rotation(RotationOutcome.UNKNOWN)
        session_id, secret = parts
        entry = self._live(session_id)
        if entry is None:
            return Rotation(RotationOutcome.UNKNOWN)
        digest = _digest(secret)
        if digest == entry.current:
            new_secret = _new_secret()
            entry.previous, entry.current = entry.current, _digest(new_secret)
            return Rotation(
                RotationOutcome.ROTATED, session_id=session_id, user_id=entry.user_id,
                role=entry.role, refresh_token=f"{session_id}.{new_secret}",
            )
        if digest == entry.previous:
            del self._sessions[session_id]
            return Rotation(RotationOutcome.REUSED, session_id=session_id, user_id=entry.user_id)
        return Rotation(RotationOutcome.UNKNOWN)

    async def revoke(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def revoke_user(self, user_id: str) -> int:
        doomed = [sid for sid, e in self._sessions.items() if e.user_id == user_id]
        for session_id in doomed:
            del self._sessions[session_id]
        return len(doomed)

    def clear(self) -> None:
        self._sessions.clear()
