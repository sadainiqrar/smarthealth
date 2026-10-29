"""The cancel follow-up workflow (spec §4.3): stop the visit timer, refund, and offer the
freed slot to the waitlist. Activities are recording fakes with the real names; the
workflows are the real ones, in the real sandbox.

Every test takes `isolated_env`: a confirmed promotion runs a booking as a child, and a
parent that starts a child and closes stops time skipping for the rest of a shared test
server (tests/README). Every visit a booking started is ended inside the worker context.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.common import WorkflowIDReusePolicy

from app.modules.scheduling.contracts import BookingRequest, FollowUpRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow, CancelFollowUpWorkflow
from tests.tiers.t2_workflow.followup_fakes import (
    World,
    finish_visits,
    follow_up_worker,
    start_visit,
)

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _request(slot_id: str | None = "slot-1") -> FollowUpRequest:
    return FollowUpRequest(
        appointment_id=str(uuid.uuid4()), slot_id=slot_id, actor_user_id=None
    )


async def _run(env, queue: str, request: FollowUpRequest) -> str:
    return await env.client.execute_workflow(
        CancelFollowUpWorkflow.run, request, id=f"cancel-{request.appointment_id}-{uuid.uuid4()}",
        task_queue=queue,
    )


def _entry(patient_id: str | None = None) -> list[str]:
    return [str(uuid.uuid4()), patient_id or str(uuid.uuid4())]


async def test_it_stops_the_visit_timer_and_refunds(isolated_env):
    world = World()
    request = _request()
    async with follow_up_worker(isolated_env, world) as (queue, release):
        await start_visit(isolated_env, queue, request.appointment_id)
        assert await _run(isolated_env, queue, request) == "released"
        ended = await finish_visits(isolated_env, release, [request.appointment_id])
    assert ended == {request.appointment_id: "cancelled"}
    assert world.refunds == [request]


async def test_with_no_visit_workflow_it_still_completes(isolated_env):
    """A booking from before slice 3, or a visit already finished: the signal finds no
    workflow, which must not fail the follow-up."""
    world = World()
    request = _request()
    async with follow_up_worker(isolated_env, world) as (queue, _):
        assert await _run(isolated_env, queue, request) == "released"
    assert world.refunds == [request]


async def test_an_appointment_that_held_no_slot_is_refunded_but_promotes_nobody(isolated_env):
    world = World(candidates=[_entry()])
    request = _request(slot_id=None)
    async with follow_up_worker(isolated_env, world) as (queue, _):
        assert await _run(isolated_env, queue, request) == "no-slot"
    assert world.refunds == [request]
    assert world.candidate_queries == [] and world.fulfilled == []


async def test_nobody_waiting_means_the_slot_is_simply_released(isolated_env):
    world = World()
    async with follow_up_worker(isolated_env, world) as (queue, _):
        assert await _run(isolated_env, queue, _request("slot-7")) == "released"
    assert world.candidate_queries == ["slot-7"]
    assert world.fulfilled == []


async def test_the_first_candidate_whose_booking_confirms_is_promoted(isolated_env):
    first, second = _entry(), _entry()
    world = World(candidates=[first, second])
    async with follow_up_worker(isolated_env, world) as (queue, release):
        result = await _run(isolated_env, queue, _request("slot-9"))
        assert result.startswith("promoted:")
        promoted = result.removeprefix("promoted:")
        await finish_visits(isolated_env, release, [promoted])
    # Only the first was tried: the slot is taken once it confirms.
    assert [r.patient_id for r in world.confirmed] == [first[1]]
    assert world.confirmed[0].appointment_id == promoted
    assert world.confirmed[0].slot_id == "slot-9"
    assert world.confirmed[0].actor_user_id == "system:waitlist"
    assert world.fulfilled == [first[0]]
    assert world.calls.index("fulfil") > world.calls.index("confirm")


async def test_a_failing_first_booking_promotes_the_second(isolated_env):
    first, second = _entry(), _entry()
    world = World(candidates=[first, second], losing_patients={first[1]})
    async with follow_up_worker(isolated_env, world) as (queue, release):
        result = await _run(isolated_env, queue, _request())
        assert result.startswith("promoted:")
        await finish_visits(isolated_env, release, [result.removeprefix("promoted:")])
    assert [r.patient_id for r in world.confirmed] == [second[1]]
    assert world.fulfilled == [second[0]], "the failed entry stays active for a later release"
    # The failed attempt compensated through the booking's own fail_booking.
    assert world.calls.count("fail_booking") == 1


async def test_every_booking_failing_promotes_nobody(isolated_env):
    entries = [_entry(), _entry()]
    world = World(candidates=entries, losing_patients={e[1] for e in entries})
    async with follow_up_worker(isolated_env, world) as (queue, _):
        assert await _run(isolated_env, queue, _request()) == "released"
    assert world.confirmed == [] and world.fulfilled == []


async def test_an_entry_already_promoted_is_skipped(isolated_env):
    """The double-promotion guard: `booking-wl-<entry>` already completed, so starting it
    again raises WorkflowAlreadyStartedError and the entry is passed over -- two releases
    racing for one entry cannot book it twice."""
    done, fresh = _entry(), _entry()
    world = World(candidates=[done, fresh])
    earlier = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=done[1], slot_id="elsewhere",
        actor_user_id="system:waitlist",
    )
    async with follow_up_worker(isolated_env, world) as (queue, release):
        await isolated_env.client.execute_workflow(
            BookAppointmentWorkflow.run, earlier, id=f"booking-wl-{done[0]}",
            task_queue=queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
        result = await _run(isolated_env, queue, _request("slot-3"))
        assert result.startswith("promoted:")
        promoted = result.removeprefix("promoted:")
        await finish_visits(isolated_env, release, [earlier.appointment_id, promoted])
    # The first confirmation is the earlier run's; the follow-up booked only `fresh`.
    assert [r.patient_id for r in world.confirmed] == [done[1], fresh[1]]
    assert world.confirmed[1].slot_id == "slot-3"
    assert world.fulfilled == [fresh[0]]


async def test_a_failed_earlier_attempt_may_be_tried_again(isolated_env):
    """The other half of ALLOW_DUPLICATE_FAILED_ONLY: an entry whose earlier booking
    *failed* is offered the next slot that frees up."""
    entry = _entry()
    world = World(candidates=[entry], losing_patients={entry[1]})
    async with follow_up_worker(isolated_env, world) as (queue, release):
        assert await _run(isolated_env, queue, _request("slot-a")) == "released"
        world.losing_patients.clear()
        result = await _run(isolated_env, queue, _request("slot-b"))
        assert result.startswith("promoted:")
        await finish_visits(isolated_env, release, [result.removeprefix("promoted:")])
    assert world.fulfilled == [entry[0]]
