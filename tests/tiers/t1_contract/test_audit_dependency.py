"""Contract for `app.api.deps.get_audit_log`.

T1 rather than T0: the subject *is* framework wiring — the `request.app.state`
attribute names, the argument order into `get_audit_collection`, and whether
`get_clock` sits in the provider's dependency graph. None of that is provable
without FastAPI, and `tests/tiers/t0_unit` is deliberately framework-free (its
`test_audit.py` asserts `app.core.audit` never imports FastAPI at all). This file
follows `test_authz.py`, which builds its own minimal `FastAPI()` for the same
reason. No real Mongo and no containers, so it stays in the fast lane.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from app.api.deps import get_audit_log
from app.core.audit import AuditEvent, AuditLog
from app.core.clock import FixedClock, get_clock
from app.db.mongo import AUDIT_COLLECTION
from app.settings import Settings

pytestmark = pytest.mark.contract

INSTANT = datetime(2026, 9, 3, 10, 30, tzinfo=UTC)
SETTINGS = Settings(mongo_db="audit-dep-test")


class _FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def insert_one(self, document: dict) -> None:
        self.documents.append(document)


class _FakeDatabase(dict):
    """`get_audit_collection` indexes twice: `client[db][collection]`."""


def _fake_mongo() -> tuple[_FakeDatabase, _FakeCollection]:
    collection = _FakeCollection()
    client = _FakeDatabase({SETTINGS.mongo_db: _FakeDatabase({AUDIT_COLLECTION: collection})})
    return client, collection


def _build_app() -> tuple[FastAPI, _FakeCollection]:
    app = FastAPI()
    client, collection = _fake_mongo()
    app.state.mongo = client
    app.state.settings = SETTINGS
    return app, collection


async def test_get_audit_log_returns_a_log_wired_to_the_apps_collection():
    """Nothing else calls this provider yet, so its `app.state` attribute names and
    its argument order into `get_audit_collection` would otherwise be verified only
    by reading — and a rename would surface as a 500 on the first live mutation."""
    app, collection = _build_app()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})

    log = get_audit_log(request, FixedClock(INSTANT))

    assert isinstance(log, AuditLog)
    await log.record(AuditEvent(entity_type="patient", entity_id="p1", action="registered"))
    assert len(collection.documents) == 1
    assert collection.documents[0]["entity_id"] == "p1"


async def test_the_audit_clock_is_replaceable_through_dependency_overrides():
    """`get_clock` must be in this provider's dependency graph, not called inline.

    An inline `get_clock()` still returns a working clock, so the provider looks
    fine — but `app.dependency_overrides[get_clock]` then has no effect on audit
    timestamps, which is the one place Clock injection is observable through a real
    HTTP request. Only resolving the dependency through FastAPI catches that.
    """
    app, collection = _build_app()

    @app.post("/mutate")
    async def mutate(log: AuditLog = Depends(get_audit_log)) -> dict[str, str]:
        await log.record(AuditEvent(entity_type="patient", entity_id="p1", action="registered"))
        return {"ok": "yes"}

    app.dependency_overrides[get_clock] = lambda: FixedClock(INSTANT)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mutate")

    assert response.status_code == 200
    assert collection.documents[0]["at"] == INSTANT
