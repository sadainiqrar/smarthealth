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

import datetime
import decimal
import enum
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.clock import Clock


def _normalise(value: Any) -> Any:
    """Convert the types BSON cannot encode, and nothing else.

    Verified against pymongo 4.17.0: `bson.encode({"d": datetime.date(2000, 1, 1)})`
    raises `InvalidDocument`, while a `datetime.datetime` encodes fine. Because audit
    writes are awaited inside the request, an unencodable value fails the whole
    mutation rather than just the audit entry — so a `date` reaching `record` is a
    500 on a business endpoint, not a lost log line.

    Only the known-unencodable types are touched. Anything else is passed through
    untouched so a genuinely unsupported type still fails loudly at insert time
    rather than being silently stringified into an audit trail nobody can trust.

    The `datetime`/`date` order below is load-bearing: `datetime.datetime` is a
    subclass of `datetime.date`, so an `isinstance(value, date)` test placed first
    would match real timestamps and stringify them.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def _normalise_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """`None` means "there was no before/after state", which is not the same as `{}`."""
    if payload is None:
        return None
    return {key: _normalise(value) for key, value in payload.items()}


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
    """Writes audit documents through an injected collection and clock.

    Known residual risk — the audit write and the business commit are not atomic.
    A mutation runs as: the service flushes to Postgres, the audit document is
    written to Mongo, then the router commits. If that final commit fails, the audit
    trail permanently records a change that never took effect, and there is no
    compensating write to retract it.

    The order is nonetheless the right way round. Reversed — commit first, audit
    second — a failed audit write would leave a real mutation with no record of who
    made it, which is the worse of the two failures and is undetectable. As written,
    a failed audit write propagates and the session rolls back, so nothing happened
    in either store.

    Accepted mitigation for Week 1: keep the flush-to-commit distance minimal, so the
    window in which a commit can fail after the audit write is as small as it can be
    without a distributed transaction. Reconciling orphaned audit entries against
    Postgres is out of scope for Week 1.
    """

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
                "before": _normalise_payload(event.before),
                "after": _normalise_payload(event.after),
                "at": self._clock.now(),
            }
        )
