"""Booking end to end: HTTP -> real Temporal -> real worker -> real Postgres and Mongo.

The worker runs in-process on this run's prefixed task queue, so the test needs no
container beyond the compose test stack.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WorkflowFailureError

from app.components import build_activities, register_all
from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import TASK_QUEUE, WORKFLOWS, build_worker, connect_client
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client, get_audit_collection
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.scheduling.booking import booking_workflow_id
from app.modules.scheduling.models import ProviderSlot, SlotStatus
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def booking_stack(db_settings):
    """A worker with real activities, plus a session factory and Temporal client.

    Not named `stack`: that is the session-scoped compose fixture `db_settings` depends
    on, and shadowing it with a function-scoped one is a pytest ScopeMismatch."""
    register_all()
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    mongo = create_mongo_client(db_settings)
    audit = AuditLog(get_audit_collection(mongo, db_settings), Clock())
    client = await connect_client(db_settings, lazy=False)
    worker = build_worker(
        client, db_settings.task_queue(TASK_QUEUE), workflows=list(WORKFLOWS),
        activities=build_activities(factory, audit, Clock()),
    )
    try:
        async with worker:
            yield factory, client
    finally:
        async with factory() as session:
            await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
        scheduling_helpers.CREATED.clear()
        await engine.dispose()
        await mongo.close()


async def _finish(client, appointment_id: str):
    return await client.get_workflow_handle(booking_workflow_id(appointment_id)).result()


async def test_a_booking_is_confirmed_end_to_end(api, token_for, booking_stack, audit_documents):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session)
    headers = token_for(UserRole.FRONT_DESK)

    accepted = await api.post(
        "/appointments", json={"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)},
        headers=headers,
    )
    assert accepted.status_code == 202
    appointment_id = accepted.json()["appointment_id"]
    assert await _finish(client, appointment_id) == "confirmed"

    read = await api.get(f"/appointments/{appointment_id}", headers=headers)
    assert read.status_code == 200
    assert read.json()["status"] == "confirmed" and read.json()["slot_id"] == str(b.slot_id)
    async with factory() as session:
        slot = await session.get(ProviderSlot, b.slot_id)
    assert slot.status is SlotStatus.BOOKED and slot.version == 2

    cursor = audit_documents.find({"entity_id": appointment_id}).sort("at", 1)
    actions = [d["action"] async for d in cursor]
    assert actions == ["booking_requested", "slot_claimed", "confirmed"]


async def test_a_repeated_request_books_once(api, token_for, booking_stack):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session)
    headers = {**token_for(UserRole.FRONT_DESK), "Idempotency-Key": uuid.uuid4().hex}
    body = {"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)}

    first = await api.post("/appointments", json=body, headers=headers)
    await _finish(client, first.json()["appointment_id"])
    second = await api.post("/appointments", json=body, headers=headers)

    assert second.status_code == 202
    assert second.json()["appointment_id"] == first.json()["appointment_id"]


async def test_a_past_slot_fails_and_leaves_the_slot_free(api, token_for, booking_stack):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session, starts_in=timedelta(hours=-1))
    headers = token_for(UserRole.FRONT_DESK)

    accepted = await api.post(
        "/appointments", json={"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)},
        headers=headers,
    )
    appointment_id = accepted.json()["appointment_id"]
    with pytest.raises(WorkflowFailureError):
        await _finish(client, appointment_id)

    read = await api.get(f"/appointments/{appointment_id}", headers=headers)
    assert read.json()["status"] == "failed"
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.FREE


async def test_two_bookings_for_one_slot_confirm_exactly_one(api, token_for, booking_stack):
    factory, client = booking_stack
    async with factory() as session:
        b = await seed_bookable(session)
    headers = token_for(UserRole.FRONT_DESK)
    body = {"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)}

    accepted = await asyncio.gather(
        api.post("/appointments", json=body, headers=headers),
        api.post("/appointments", json=body, headers=headers),
    )
    ids = [r.json()["appointment_id"] for r in accepted]
    outcomes = await asyncio.gather(*(_finish(client, i) for i in ids), return_exceptions=True)

    assert sorted(type(o).__name__ for o in outcomes) == ["WorkflowFailureError", "str"]
    # A list comprehension, not a generator: `await` inside a generator expression makes
    # it an async generator, which `sorted` cannot iterate.
    statuses = sorted(
        [(await api.get(f"/appointments/{i}", headers=headers)).json()["status"] for i in ids]
    )
    assert statuses == ["confirmed", "failed"]
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.BOOKED
