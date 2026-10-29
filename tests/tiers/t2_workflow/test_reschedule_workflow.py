"""The reschedule workflow (spec §4.4, D-14): book the new slot first, release the old one
only if that confirmed. Activities are recording fakes with the real names; the workflows
are the real ones, in the real sandbox. Every test takes `isolated_env` and ends every visit
a booking started inside the worker context (tests/README, time-skipping pitfalls).
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.client import WorkflowExecutionStatus

from app.modules.scheduling.contracts import RescheduleRequest
from app.modules.scheduling.workflows import RescheduleWorkflow
from tests.tiers.t2_workflow.followup_fakes import (
    World,
    finish_visits,
    follow_up_worker,
    start_visit,
)

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _request() -> RescheduleRequest:
    return RescheduleRequest(
        old_appointment_id=str(uuid.uuid4()), new_appointment_id=str(uuid.uuid4()),
        patient_id=str(uuid.uuid4()), new_slot_id=str(uuid.uuid4()),
        actor_user_id="someone", reason="clash",
    )


async def _run(env, queue: str, request: RescheduleRequest) -> str:
    return await env.client.execute_workflow(
        RescheduleWorkflow.run, request, id=f"reschedule-{request.new_appointment_id}",
        task_queue=queue,
    )


async def test_it_books_first_then_releases_the_old_slot(isolated_env):
    request = _request()
    waiting = [str(uuid.uuid4()), str(uuid.uuid4())]
    world = World(candidates=[waiting], released_slot_id="old-slot")
    async with follow_up_worker(isolated_env, world) as (queue, release):
        await start_visit(isolated_env, queue, request.old_appointment_id)
        assert await _run(isolated_env, queue, request) == "rescheduled"
        confirmed = [r.appointment_id for r in world.confirmed]
        # The new booking, then the waitlisted patient the released slot went to.
        assert len(confirmed) == 2
        ended = await finish_visits(
            isolated_env, release, [request.old_appointment_id, *confirmed]
        )

    # The new booking is the first confirmation, and it carries the link to the old one.
    assert confirmed[0] == request.new_appointment_id
    assert world.bookings[0].rescheduled_from_id == request.old_appointment_id
    assert world.bookings[0].patient_id == request.patient_id
    assert world.bookings[0].slot_id == request.new_slot_id
    # D-14: the old appointment is touched only after the new one confirmed.
    assert world.calls.index("confirm") < world.calls.index("reschedule_complete")
    assert world.reschedules == [request]
    # Its visit timer is stopped, and the slot it released goes to the waitlist.
    assert ended[request.old_appointment_id] == "cancelled"
    assert world.candidate_queries == ["old-slot"]
    assert world.fulfilled == [waiting[0]]
    assert world.refunds == [], "a reschedule moves the booking; nothing is refunded"


async def test_a_failed_new_booking_leaves_the_old_appointment_alone(isolated_env):
    request = _request()
    world = World(losing_patients={request.patient_id})
    async with follow_up_worker(isolated_env, world) as (queue, release):
        await start_visit(isolated_env, queue, request.old_appointment_id)
        assert await _run(isolated_env, queue, request) == "failed"
        # The old visit timer was never signalled, so it is still running to be ended.
        handle = isolated_env.client.get_workflow_handle(
            f"visit-{request.old_appointment_id}"
        )
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        await finish_visits(isolated_env, release, [request.old_appointment_id])
    assert "reschedule_complete" not in world.calls
    assert world.candidate_queries == []
    assert world.calls.count("fail_booking") == 1


async def test_a_superseded_reschedule_releases_and_promotes_nothing(isolated_env):
    """The old appointment was cancelled, or the patient checked in, while the new booking
    ran: `reschedule.complete` writes nothing and the new booking stands."""
    request = _request()
    world = World(reschedule_outcome="superseded", candidates=[[str(uuid.uuid4())] * 2])
    async with follow_up_worker(isolated_env, world) as (queue, release):
        assert await _run(isolated_env, queue, request) == "superseded"
        await finish_visits(
            isolated_env, release, [r.appointment_id for r in world.confirmed]
        )
    assert world.candidate_queries == [] and world.fulfilled == []
    assert world.reschedules == [request]
