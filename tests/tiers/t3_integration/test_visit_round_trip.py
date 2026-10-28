"""The visit lifecycle end to end: HTTP -> real Temporal -> real worker -> real Postgres
and Mongo. The worker runs in-process on this run's prefixed task queue, like
`test_booking_round_trip`.

Every visit workflow a test starts is finished by the test (driven to completion, or
cancelled in teardown) so none is left running on the shared test Temporal.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WorkflowExecutionStatus

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
from app.modules.scheduling.service import get_appointment
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import confirmed_appointment, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def visit_stack(db_settings):
    """A worker with real activities, plus a session factory and Temporal client. The
    no-show grace is generous: nothing here is meant to time out."""
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


async def _until(read, done, *, timeout: float = 20.0):
    """Poll `read()` until `done(value)`, rather than sleeping blindly: the write happens
    in a workflow, after the 202."""
    deadline = asyncio.get_running_loop().time() + timeout
    value = await read()
    while not done(value):
        assert asyncio.get_running_loop().time() < deadline, f"timed out; last saw {value!r}"
        await asyncio.sleep(0.2)
        value = await read()
    return value


def _visit_status(api, appointment_id, headers):
    async def read():
        response = await api.get(f"/appointments/{appointment_id}/visit", headers=headers)
        return response.json().get("status") if response.status_code == 200 else None

    return read


async def test_a_visit_runs_from_booking_to_completion(
    api, token_for, visit_stack, audit_documents
):
    factory, client, started = visit_stack
    async with factory() as session:
        b = await seed_bookable(session)
    desk = token_for(UserRole.FRONT_DESK)
    provider = token_for(UserRole.PROVIDER)

    accepted = await api.post(
        "/appointments", json={"patient_id": str(b.patient_id), "slot_id": str(b.slot_id)},
        headers=desk,
    )
    appointment_id = accepted.json()["appointment_id"]
    started.append(appointment_id)

    async def appointment_status():
        return (await api.get(f"/appointments/{appointment_id}", headers=desk)).json()["status"]

    await _until(appointment_status, lambda s: s == "confirmed")
    # Booked, not yet checked in: there is no visit to read.
    no_visit = await api.get(f"/appointments/{appointment_id}/visit", headers=desk)
    assert no_visit.status_code == 404

    # Booking started the visit workflow itself; check-in only signals it.
    check_in = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert check_in.status_code == 202
    assert check_in.json() == {"appointment_id": appointment_id, "action": "check_in"}
    await _until(_visit_status(api, appointment_id, desk), lambda s: s == "checked_in")

    # The precheck sees the visit, so the repeat is a synchronous 409.
    again = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert again.status_code == 409
    early = await api.post(f"/appointments/{appointment_id}/complete", headers=provider)
    assert early.status_code == 409
    # Role gates: front desk does not treat patients.
    forbidden = await api.post(f"/appointments/{appointment_id}/seen", headers=desk)
    assert forbidden.status_code == 403

    seen = await api.post(f"/appointments/{appointment_id}/seen", headers=provider)
    assert seen.status_code == 202
    await _until(_visit_status(api, appointment_id, desk), lambda s: s == "in_progress")
    complete = await api.post(f"/appointments/{appointment_id}/complete", headers=provider)
    assert complete.status_code == 202
    await _until(_visit_status(api, appointment_id, desk), lambda s: s == "completed")
    await _until(appointment_status, lambda s: s == "completed")

    visit = (await api.get(f"/appointments/{appointment_id}/visit", headers=desk)).json()
    assert visit["checked_in_at"] and visit["seen_at"] and visit["completed_at"]
    assert visit["checked_in_at"] <= visit["seen_at"] <= visit["completed_at"]

    # The workflow ended by itself, and the slot stayed booked (R-2: nothing frees it).
    handle = client.get_workflow_handle(visit_workflow_id(appointment_id))
    assert await handle.result() == "completed"
    async with factory() as session:
        assert (await session.get(ProviderSlot, b.slot_id)).status is SlotStatus.BOOKED
    # A finished visit takes no more actions.
    after = await api.post(f"/appointments/{appointment_id}/complete", headers=provider)
    assert after.status_code == 409

    cursor = audit_documents.find({"entity_type": "visit"}).sort("at", 1)
    assert [d["action"] async for d in cursor] == ["checked_in", "seen", "completed"]


async def test_a_booking_confirmed_before_the_visit_workflow_existed_can_still_check_in(
    api, token_for, visit_stack
):
    """Signal-with-start: this appointment was confirmed through the service functions,
    so no visit workflow was ever started for it -- as for a booking confirmed before
    slice 3. The check-in starts one."""
    factory, client, started = visit_stack
    b, appointment_id = await confirmed_appointment(factory)
    started.append(str(appointment_id))
    desk = token_for(UserRole.FRONT_DESK)

    response = await api.post(f"/appointments/{appointment_id}/check-in", headers=desk)
    assert response.status_code == 202
    await _until(_visit_status(api, appointment_id, desk), lambda s: s == "checked_in")
    async with factory() as session:
        appointment = await get_appointment(session, appointment_id)
        assert appointment.status is AppointmentStatus.CONFIRMED


async def test_an_unknown_appointment_is_a_404_before_anything_is_signalled(
    api, token_for, visit_stack
):
    response = await api.post(
        f"/appointments/{uuid.uuid4()}/check-in", headers=token_for(UserRole.FRONT_DESK)
    )
    assert response.status_code == 404
