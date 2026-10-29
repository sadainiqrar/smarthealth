"""The visit lifecycle end to end: HTTP -> real Postgres and Mongo, with real Temporal and
a real worker for the no-show timer. The worker runs in-process on this run's prefixed
task queue, like `test_booking_round_trip`.

The endpoints write synchronously (D-12), so every assertion on a visit action is made on
the response itself, not by polling. Every visit workflow a test starts is finished by
the test or cancelled in teardown, so none is left running on the shared test Temporal.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
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
from app.modules.scheduling.models import AppointmentStatus, ProviderSlot, SlotStatus
from app.modules.scheduling.service import get_appointment, get_visit
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import confirmed_appointment, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@contextlib.asynccontextmanager
async def _visit_stack(db_settings, no_show_grace: timedelta):
    """A worker with real activities, plus a session factory and Temporal client."""
    register_all()
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    mongo = create_mongo_client(db_settings)
    audit = AuditLog(get_audit_collection(mongo, db_settings), Clock())
    client = await connect_client(db_settings, lazy=False)
    worker = build_worker(
        client, db_settings.task_queue(TASK_QUEUE), workflows=list(WORKFLOWS),
        activities=build_activities(factory, audit, Clock(), no_show_grace),
    )
    started: list[str] = []
    try:
        async with worker:
            yield factory, client, started
            # Whatever a test did not finish: cancel it, inside the worker context so the
            # workflow can process the signal and end.
            for appointment_id in started:
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


@pytest.fixture
async def visit_stack(db_settings):
    """The no-show grace is generous: nothing here is meant to time out."""
    async with _visit_stack(db_settings, timedelta(minutes=15)) as stack:
        yield stack


async def _until(read, done, *, timeout: float = 30.0):
    """Poll `read()` until `done(value)`, rather than sleeping blindly."""
    deadline = asyncio.get_running_loop().time() + timeout
    value = await read()
    while not done(value):
        assert asyncio.get_running_loop().time() < deadline, f"timed out; last saw {value!r}"
        await asyncio.sleep(0.2)
        value = await read()
    return value


def _describe_status(handle):
    async def read():
        try:
            return (await handle.describe()).status
        except RPCError:
            return None  # not started yet

    return read


async def _book(api, b, headers, started) -> str:
    accepted = await api.post(
        "/appointments", json={"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)},
        headers=headers,
    )
    assert accepted.status_code == 202, accepted.text
    appointment_id = accepted.json()["appointment_id"]
    started.append(appointment_id)

    async def status():
        return (await api.get(f"/appointments/{appointment_id}", headers=headers)).json()[
            "status"
        ]

    await _until(status, lambda s: s == "confirmed")
    return appointment_id


async def test_a_visit_runs_from_booking_to_completion(
    api, token_for, visit_stack, audit_documents
):
    factory, client, started = visit_stack
    async with factory() as session:
        b = await seed_bookable(session)
    desk_id, provider_id = str(uuid.uuid4()), str(uuid.uuid4())
    desk = token_for(UserRole.FRONT_DESK, desk_id)
    provider = token_for(UserRole.PROVIDER, provider_id)
    appointment_id = await _book(api, b, desk, started)
    # Booked, not yet checked in: there is no visit to read.
    no_visit = await api.get(f"/appointments/{appointment_id}/visit", headers=desk)
    assert no_visit.status_code == 404
    # `confirmed` is written by the booking's last activity; the visit timer starts just
    # after it. Wait for the timer, so the signal path is what this test observes. (A
    # check-in landing in that gap is recorded all the same; its signal finds no
    # workflow, and the timer ends "visit_exists" at the deadline -- the database wins.)
    handle = client.get_workflow_handle(visit_workflow_id(appointment_id))
    await _until(_describe_status(handle), lambda s: s == WorkflowExecutionStatus.RUNNING)

    # 200 with the visit: it is recorded before the response leaves.
    check_in = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert check_in.status_code == 200, check_in.text
    assert check_in.json()["status"] == "checked_in"
    assert check_in.json()["appointment_id"] == appointment_id
    # The post-commit signal reached the timer the booking started, which ends early.
    assert await asyncio.wait_for(handle.result(), 20) == "checked_in"

    # Strict: a repeat is refused, and the order is enforced.
    again = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert again.status_code == 409
    early = await api.post(f"/appointments/{appointment_id}/complete", headers=provider)
    assert early.status_code == 409
    # Role gates: front desk does not treat patients.
    assert (await api.post(f"/appointments/{appointment_id}/seen", headers=desk)).status_code == 403

    seen = await api.post(f"/appointments/{appointment_id}/seen", headers=provider)
    assert seen.status_code == 200 and seen.json()["status"] == "in_progress"
    complete = await api.post(f"/appointments/{appointment_id}/complete", headers=provider)
    assert complete.status_code == 200 and complete.json()["status"] == "completed"
    visit = complete.json()
    assert visit["checked_in_at"] <= visit["seen_at"] <= visit["completed_at"]

    appointment = (await api.get(f"/appointments/{appointment_id}", headers=desk)).json()
    assert appointment["status"] == "completed"
    async with factory() as session:
        # R-2: a completed appointment's slot stays booked.
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.BOOKED
    after = await api.post(f"/appointments/{appointment_id}/complete", headers=provider)
    assert after.status_code == 409

    # `_id` breaks ties: Mongo keeps `at` to the millisecond, and two requests can share one.
    cursor = audit_documents.find({"entity_type": "visit"}).sort([("at", 1), ("_id", 1)])
    trail = [(d["action"], d["actor_user_id"]) async for d in cursor]
    assert trail == [
        ("checked_in", desk_id), ("seen", provider_id), ("completed", provider_id)
    ]


async def test_a_booking_confirmed_before_the_visit_workflow_existed_can_still_check_in(
    api, token_for, visit_stack
):
    """This appointment was confirmed through the service functions, so it has no visit
    workflow -- as for a booking confirmed before slice 3. The check-in is recorded and
    answers 200; the signal finds no workflow, which is not an error (the documented gap:
    such an appointment is never automatically marked no-show)."""
    factory, client, _ = visit_stack
    _, appointment_id = await confirmed_appointment(factory)
    desk = token_for(UserRole.FRONT_DESK)

    response = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "checked_in"
    async with factory() as session:
        assert (await get_appointment(session, appointment_id)).status is (
            AppointmentStatus.CONFIRMED
        )
        assert await get_visit(session, appointment_id) is not None


@pytest.fixture
async def no_show_stack(db_settings):
    """No grace: the deadline is the slot's end, so a seconds-long slot times out fast."""
    async with _visit_stack(db_settings, timedelta(0)) as stack:
        yield stack


async def test_nobody_checking_in_becomes_a_no_show_on_the_real_timer(
    api, token_for, no_show_stack
):
    """The whole no-show path on real infrastructure: a booking confirms and starts its
    visit, a real Temporal timer fires at the slot's end, the real `no_show` activity
    marks the appointment under its lock, and the workflow returns the database's
    answer. The slot stays `booked` (R-2), and a check-in arriving afterwards is refused
    (409) -- staff are told, rather than accepted and silently overruled as before.

    The slot starts a few seconds out so the booking can claim it before it starts
    (R-3), and lasts one second."""
    factory, client, started = no_show_stack
    async with factory() as session:
        b = await seed_bookable(
            session, starts_in=timedelta(seconds=3), duration=timedelta(seconds=1)
        )
    desk = token_for(UserRole.FRONT_DESK)
    appointment_id = await _book(api, b, desk, started)

    async def status():
        return (await api.get(f"/appointments/{appointment_id}", headers=desk)).json()[
            "status"
        ]

    await _until(status, lambda s: s == "no_show", timeout=30)
    handle = client.get_workflow_handle(visit_workflow_id(appointment_id))
    assert await asyncio.wait_for(handle.result(), 10) == "no_show"
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.BOOKED
        assert await get_visit(session, uuid.UUID(appointment_id)) is None
    late = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert late.status_code == 409


async def test_an_unknown_appointment_is_a_404(api, token_for, visit_stack):
    response = await api.post(
        f"/appointments/{uuid.uuid4()}/check-in", headers=token_for(UserRole.FRONT_DESK)
    )
    assert response.status_code == 404
