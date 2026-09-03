"""Audit trail writes.

Mongo owns the audit trail: append-only, high volume, and a before/after payload whose
shape differs per entity and per action (foundation spec section 3.4).

Writes are **awaited**, not fire-and-forget. A dropped audit entry is invisible and
unrecoverable; a failed request is neither.

This module has zero framework imports on purpose, for the same reason as
`app.core.errors`: importing it must not pull FastAPI, Starlette, or the ASGI stack
into a process that has no business loading them (a Temporal worker, a Celery task, a
plain script). It does not import `app.db.mongo` either — that module imports
`pymongo`, which is a real dependency this module has no need of: `AuditLog` only
needs an object with an async `insert_one`, expressed here as `_Collection`, so a
caller can hand it a real Mongo collection, a fake, or anything else that fits the
shape. The HTTP-layer wiring that supplies the real collection lives in
`app.api.deps`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.clock import Clock


@dataclass(frozen=True)
class AuditEvent:
    """One recorded change. Frozen: a mutable record is not a record."""

    entity_type: str
    entity_id: str
    action: str
    actor_user_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class _Collection(Protocol):
    """The slice of a Mongo collection this module uses, so tests can fake it."""

    async def insert_one(self, document: dict[str, Any]) -> Any: ...


class AuditLog:
    def __init__(self, collection: _Collection, clock: Clock) -> None:
        self._collection = collection
        self._clock = clock

    async def record(self, event: AuditEvent) -> None:
        await self._collection.insert_one(
            {
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "action": event.action,
                "actor_user_id": event.actor_user_id,
                "before": event.before,
                "after": event.after,
                "at": self._clock.now(),
            }
        )
