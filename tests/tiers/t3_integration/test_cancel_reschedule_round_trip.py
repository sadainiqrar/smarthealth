"""Cancel, reschedule and the waitlist end to end: HTTP -> real Postgres and Mongo, with real
Temporal and an in-process worker running the real activities on this run's prefixed task
queue, like `test_booking_round_trip` and `test_visit_round_trip`.

Cancel answers from its own transaction, so those assertions are made on the response and
the rows. What follows it (the follow-up workflow) and a reschedule are asynchronous, so
those are awaited by polling or on the workflow's result, never by sleeping. Every visit
timer a booking started is cancelled in teardown, inside the worker context, so none is
left running on the shared test Temporal.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from app.components import build_activities, register_all
from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import TASK_QUEUE, WORKFLOWS, build_worker, connect_client
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client, get_audit_collection
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.scheduling.contracts import visit_workflow_id
from app.modules.scheduling.followups import cancel_workflow_id, reschedule_workflow_id
from app.modules.scheduling.models import Appointment, AppointmentStatus, ProviderSlot, SlotStatus
from app.modules.scheduling.service import get_appointment
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import add_slot, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def flow_stack(db_settings):
    """A worker with real activities, plus a session factory and Temporal client (not named
    `stack`: that is the session-scoped compose fixture). The no-show
    grace is generous: nothing here is meant to time out."""
    register_all()
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    mongo = create_mongo_client(db_settings)
    audit = AuditLog(get_audit_collection(mongo, db_settings), Clock())
    client = await connect_client(db_settings, lazy=False)
    worker = build_worker(
        client, db_settings.task_queue(TASK_QUEUE), workflows=list(WORKFLOWS),
        activities=build_activities(factory, audit, Clock(), timedelta(minutes=15)),
    )
    try:
        async with worker:
            yield factory, client
            # Whatever visit timer is still running -- the test's own bookings, a
            # reschedule's new appointment, a promoted one -- is cancelled here, inside the
            # worker context so the workflow can process the signal and end.
            async with factory() as session:
                patients = [b.patient_id for b in scheduling_helpers.CREATED]
                ids = list(
                    await session.scalars(
                        select(Appointment.id).where(Appointment.patient_id.in_(patients))
                    )
                )
            for appointment_id in ids:
                handle = client.get_workflow_handle(visit_workflow_id(appointment_id))
                with contextlib.suppress(Exception):
                    if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                        await handle.signal("cancel")
                        await asyncio.wait_for(handle.result(), 10)
    finally:
        async with factory() as session:
            await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
        scheduling_helpers.CREATED.clear()
        await engine.dispose()
        await mongo.close()


async def _until(read, done, *, timeout: float = 30.0):
    """Poll `read()` until `done(value)`, rather than sleeping blindly."""
    deadline = asyncio.get_running_loop().time() + timeout
    value = await read()
    while not done(value):
        assert asyncio.get_running_loop().time() < deadline, f"timed out; last saw {value!r}"
        await asyncio.sleep(0.2)
        value = await read()
    return value


def _status_of(api, appointment_id: str, headers):
    async def read():
        return (await api.get(f"/appointments/{appointment_id}", headers=headers)).json()[
            "status"
        ]

    return read


def _workflow_status(handle):
    async def read():
        try:
            return (await handle.describe()).status
        except RPCError:
            return None  # not started yet

    return read


async def _book(api, client, patient_id, slot_id, headers) -> str:
    """Book to `confirmed` and wait for the visit timer the confirmation starts, so a later
    cancel's signal has a workflow to land on."""
    accepted = await api.post(
        "/appointments", json={"patient_id": str(patient_id), "slot_id": str(slot_id)},
        headers=headers,
    )
    assert accepted.status_code == 202, accepted.text
    appointment_id = accepted.json()["appointment_id"]
    await _until(_status_of(api, appointment_id, headers), lambda s: s == "confirmed")
    visit = client.get_workflow_handle(visit_workflow_id(appointment_id))
    await _until(_workflow_status(visit), lambda s: s == WorkflowExecutionStatus.RUNNING)
    return appointment_id


async def _slot_status(factory, slot_id) -> SlotStatus:
    async with factory() as session:
        return (await session.get(ProviderSlot, slot_id)).status


# --- cancel ---------------------------------------------------------------------------


async def test_a_cancel_frees_the_slot_and_runs_its_follow_up(
    api, token_for, flow_stack, audit_documents
):
    factory, client = flow_stack
    async with factory() as session:
        b = await seed_bookable(session)
    desk_id = str(uuid.uuid4())
    desk = token_for(UserRole.FRONT_DESK, desk_id)
    appointment_id = await _book(api, client, b.patient_id, b.slot_id, desk)
    assert await _slot_status(factory, b.slot_id) is SlotStatus.BOOKED

    cancel = await api.post(
        f"/appointments/{appointment_id}/cancel", json={"reason": "cannot make it"},
        headers=desk,
    )

    # 200 with the cancelled appointment: the paired write committed before the response.
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    assert await _slot_status(factory, b.slot_id) is SlotStatus.FREE
    async with factory() as session:
        row = await get_appointment(session, uuid.UUID(appointment_id))
    assert row.status is AppointmentStatus.CANCELLED
    assert row.cancellation_reason == "cannot make it" and row.cancelled_at is not None

    # The follow-up ran: nobody was waiting, and the visit timer was stopped.
    follow_up = client.get_workflow_handle(cancel_workflow_id(appointment_id))
    assert await asyncio.wait_for(follow_up.result(), 20) == "released"
    visit = client.get_workflow_handle(visit_workflow_id(appointment_id))
    assert await asyncio.wait_for(visit.result(), 20) == "cancelled"

    # Strict: a second cancel is refused, not silently accepted.
    again = await api.post(
        f"/appointments/{appointment_id}/cancel", json={"reason": "again"}, headers=desk
    )
    assert again.status_code == 409

    cursor = audit_documents.find({"entity_id": appointment_id}).sort([("at", 1), ("_id", 1)])
    trail = [(d["action"], d["actor_user_id"]) async for d in cursor]
    assert trail == [
        ("booking_requested", desk_id), ("slot_claimed", desk_id), ("confirmed", desk_id),
        ("cancelled", desk_id),
    ]


async def test_the_freed_slot_can_be_booked_again(api, token_for, flow_stack):
    factory, client = flow_stack
    async with factory() as session:
        b = await seed_bookable(session)
    desk = token_for(UserRole.FRONT_DESK)
    first = await _book(api, client, b.patient_id, b.slot_id, desk)
    await api.post(f"/appointments/{first}/cancel", json={"reason": "x"}, headers=desk)

    # The partial unique index and `provider_slots.status` both let go (R-2).
    second = await _book(api, client, b.patient_id, b.slot_id, desk)
    assert second != first
    assert await _slot_status(factory, b.slot_id) is SlotStatus.BOOKED


# --- waitlist -------------------------------------------------------------------------


async def test_a_cancelled_slot_is_promoted_to_the_waitlist(api, token_for, flow_stack):
    factory, client = flow_stack
    # B is seeded first: `purge` walks the rows in creation order, and B's promoted
    # appointment holds A's slot, so it must go before A's slots do.
    async with factory() as session:
        waiting = await seed_bookable(session, starts_in=timedelta(days=5))
        b = await seed_bookable(session)
    desk = token_for(UserRole.FRONT_DESK)
    async with factory() as session:
        slot = await session.get(ProviderSlot, b.slot_id)
        start = slot.starts_at

    joined = await api.post(
        "/waitlist",
        json={
            "patient_id": str(waiting.patient_id), "provider_id": str(b.provider_id),
            "window_start": (start - timedelta(hours=1)).isoformat(),
            "window_end": (start + timedelta(hours=1)).isoformat(),
        },
        headers=desk,
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["status"] == "active"
    entry_id = joined.json()["id"]

    appointment_id = await _book(api, client, b.patient_id, b.slot_id, desk)
    cancel = await api.post(
        f"/appointments/{appointment_id}/cancel", json={"reason": "moving"}, headers=desk
    )
    assert cancel.status_code == 200

    follow_up = client.get_workflow_handle(cancel_workflow_id(appointment_id))
    result = await asyncio.wait_for(follow_up.result(), 30)
    assert result.startswith("promoted:")
    promoted = result.removeprefix("promoted:")

    # The waiting patient now holds the slot, confirmed, and the entry is spent.
    read = await api.get(f"/appointments/{promoted}", headers=desk)
    assert read.json()["status"] == "confirmed"
    assert read.json()["patient_id"] == str(waiting.patient_id)
    assert read.json()["slot_id"] == str(b.slot_id)
    assert await _slot_status(factory, b.slot_id) is SlotStatus.BOOKED
    listed = await api.get(f"/waitlist?patient_id={waiting.patient_id}", headers=desk)
    assert [(e["id"], e["status"]) for e in listed.json()["items"]] == [(entry_id, "fulfilled")]
    # The promotion booking is the one `booking-wl-<entry>` names, so it cannot run twice.
    booking = client.get_workflow_handle(f"booking-wl-{entry_id}")
    assert (await booking.describe()).status == WorkflowExecutionStatus.COMPLETED


# --- reschedule -----------------------------------------------------------------------


async def test_a_reschedule_books_the_new_slot_then_frees_the_old(
    api, token_for, flow_stack, audit_documents
):
    factory, client = flow_stack
    async with factory() as session:
        b = await seed_bookable(session)
        second_slot = await add_slot(session, b)
    desk_id = str(uuid.uuid4())
    desk = token_for(UserRole.FRONT_DESK, desk_id)
    old_id = await _book(api, client, b.patient_id, b.slot_id, desk)

    headers = {**desk, "Idempotency-Key": uuid.uuid4().hex}
    body = {"new_slot_id": str(second_slot), "reason": "clash"}
    accepted = await api.post(f"/appointments/{old_id}/reschedule", json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    new_id = accepted.json()["appointment_id"]
    assert accepted.json()["rescheduled_from"] == old_id

    workflow = client.get_workflow_handle(reschedule_workflow_id(new_id))
    assert await asyncio.wait_for(workflow.result(), 30) == "rescheduled"

    new = await api.get(f"/appointments/{new_id}", headers=desk)
    assert new.json()["status"] == "confirmed" and new.json()["slot_id"] == str(second_slot)
    async with factory() as session:
        new_row = await get_appointment(session, uuid.UUID(new_id))
        old_row = await get_appointment(session, uuid.UUID(old_id))
    assert str(new_row.rescheduled_from_id) == old_id
    assert old_row.status is AppointmentStatus.RESCHEDULED
    assert await _slot_status(factory, b.slot_id) is SlotStatus.FREE
    assert await _slot_status(factory, second_slot) is SlotStatus.BOOKED
    # The old appointment's visit timer was stopped; the new one's is running.
    old_visit = client.get_workflow_handle(visit_workflow_id(old_id))
    assert await asyncio.wait_for(old_visit.result(), 20) == "cancelled"
    new_visit = client.get_workflow_handle(visit_workflow_id(new_id))
    assert (await new_visit.describe()).status == WorkflowExecutionStatus.RUNNING

    # A repeat of the accepted request answers as the first did, though the old appointment
    # is `rescheduled` now and a fresh request for it would be a 409.
    again = await api.post(f"/appointments/{old_id}/reschedule", json=body, headers=headers)
    assert again.status_code == 202 and again.json() == accepted.json()
    fresh = await api.post(
        f"/appointments/{old_id}/reschedule", json=body, headers=desk
    )
    assert fresh.status_code == 409

    cursor = audit_documents.find({"entity_id": old_id}).sort([("at", 1), ("_id", 1)])
    assert [d["action"] async for d in cursor] == [
        "booking_requested", "slot_claimed", "confirmed", "rescheduled",
    ]


async def test_a_reschedule_to_a_taken_slot_leaves_the_original(api, token_for, flow_stack):
    factory, client = flow_stack
    async with factory() as session:
        mine = await seed_bookable(session)
        theirs = await seed_bookable(session, starts_in=timedelta(days=3))
    desk = token_for(UserRole.FRONT_DESK)
    old_id = await _book(api, client, mine.patient_id, mine.slot_id, desk)
    await _book(api, client, theirs.patient_id, theirs.slot_id, desk)

    accepted = await api.post(
        f"/appointments/{old_id}/reschedule", json={"new_slot_id": str(theirs.slot_id)},
        headers=desk,
    )
    assert accepted.status_code == 202, accepted.text
    new_id = accepted.json()["appointment_id"]

    workflow = client.get_workflow_handle(reschedule_workflow_id(new_id))
    assert await asyncio.wait_for(workflow.result(), 30) == "failed"

    # D-14: the new booking failed and compensated; the old appointment never moved.
    new = await api.get(f"/appointments/{new_id}", headers=desk)
    assert new.json()["status"] == "failed"
    old = await api.get(f"/appointments/{old_id}", headers=desk)
    assert old.json()["status"] == "confirmed" and old.json()["slot_id"] == str(mine.slot_id)
    assert await _slot_status(factory, mine.slot_id) is SlotStatus.BOOKED
    # The other patient's slot is untouched too.
    assert await _slot_status(factory, theirs.slot_id) is SlotStatus.BOOKED
    # And the old visit timer was never told to stop.
    visit = client.get_workflow_handle(visit_workflow_id(old_id))
    assert (await visit.describe()).status == WorkflowExecutionStatus.RUNNING


async def test_two_concurrent_reschedules_of_one_appointment_confirm_one(
    api, token_for, flow_stack
):
    """The reviewer's probe: two reschedules of one appointment, to different slots,
    accepted together. Both pass the router's precheck before either writes, so only the
    database can refuse the second -- `ux_appointments_live_reschedule` (D-5). Exactly one
    new appointment confirms; the other's booking fails at `create_pending` and leaves no
    slot behind; the old appointment is `rescheduled` once, its slot free."""
    factory, client = flow_stack
    async with factory() as session:
        b = await seed_bookable(session)
        slot_a = await add_slot(session, b, starts_in=timedelta(days=2))
        slot_b = await add_slot(session, b, starts_in=timedelta(days=3))
    desk = token_for(UserRole.FRONT_DESK)
    old_id = await _book(api, client, b.patient_id, b.slot_id, desk)

    responses = await asyncio.gather(
        *(
            api.post(
                f"/appointments/{old_id}/reschedule", json={"new_slot_id": str(slot)},
                headers={**desk, "Idempotency-Key": uuid.uuid4().hex},
            )
            for slot in (slot_a, slot_b)
        )
    )
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    new_ids = [r.json()["appointment_id"] for r in responses]
    results = [
        await asyncio.wait_for(
            client.get_workflow_handle(reschedule_workflow_id(new_id)).result(), 30
        )
        for new_id in new_ids
    ]

    assert sorted(results) in (["failed", "rescheduled"], ["rescheduled", "superseded"])
    winner = results.index("rescheduled")
    loser = 1 - winner
    statuses = [
        (await api.get(f"/appointments/{new_id}", headers=desk)).json()["status"]
        for new_id in new_ids
    ]
    assert statuses[winner] == "confirmed"
    assert statuses[loser] == "failed"
    async with factory() as session:
        old_row = await get_appointment(session, uuid.UUID(old_id))
        live = list(
            await session.scalars(
                select(Appointment.id).where(
                    Appointment.patient_id == b.patient_id,
                    Appointment.status == AppointmentStatus.CONFIRMED,
                )
            )
        )
    assert old_row.status is AppointmentStatus.RESCHEDULED
    assert [str(i) for i in live] == [new_ids[winner]], "one confirmed appointment, not two"
    slots = (slot_a, slot_b)
    assert await _slot_status(factory, b.slot_id) is SlotStatus.FREE
    assert await _slot_status(factory, slots[winner]) is SlotStatus.BOOKED
    assert await _slot_status(factory, slots[loser]) is SlotStatus.FREE
