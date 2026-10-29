"""The visit lifecycle at the workflow layer: signals drive check-in -> seen -> complete,
a durable timer drives no-show. Activities are recording fakes with the real names; the
workflow is the real one, in the real sandbox.

Every workflow started here finishes inside the worker context (signalled to completion,
cancelled, or ended by its timer): one left on a queue nobody polls stops the
time-skipping server from skipping for the rest of the session.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio import activity

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import VisitRequest, VisitSignal
from app.modules.scheduling.workflows import VisitWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

STAFF = VisitSignal(actor_user_id="staff-1")


def _fakes(
    env, calls: list[str], *, deadline: timedelta = timedelta(hours=2),
    no_show: str = "no_show",
):
    @activity.defn(name="visit.deadline")
    async def fake_deadline(request: VisitRequest) -> str:
        calls.append("deadline")
        # The server's virtual clock, not the wall clock: earlier tests skip time forward,
        # and a wall-clock deadline would already be in the past.
        return ((await env.get_current_time()) + deadline).isoformat()

    @activity.defn(name="visit.no_show")
    async def fake_no_show(request: VisitRequest) -> str:
        calls.append("no_show")
        return no_show

    def two_arg(name: str):
        @activity.defn(name=f"visit.{name}")
        async def fake(request: VisitRequest, signal: VisitSignal) -> None:
            calls.append(name)

        return fake

    return [fake_deadline, fake_no_show, *(two_arg(n) for n in ("check_in", "seen", "complete"))]


async def _start(env, queue: str):
    return await env.client.start_workflow(
        VisitWorkflow.run, VisitRequest(appointment_id=str(uuid.uuid4())),
        id=f"visit-{uuid.uuid4()}", task_queue=queue,
    )


def _worker(env, queue, calls, **kwargs):
    return build_worker(
        env.client, queue, workflows=[VisitWorkflow], activities=_fakes(env, calls, **kwargs)
    )


def _queue() -> str:
    return f"t2-visit-{uuid.uuid4().hex[:8]}"


async def test_signals_drive_the_visit_to_completion_in_order(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.check_in, STAFF)
        await handle.signal(VisitWorkflow.seen, STAFF)
        await handle.signal(VisitWorkflow.complete, STAFF)
        assert await handle.result() == "completed"
    assert calls == ["deadline", "check_in", "seen", "complete"]


async def test_signals_arriving_out_of_order_are_still_applied_in_order(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.complete, STAFF)
        await handle.signal(VisitWorkflow.seen, STAFF)
        await handle.signal(VisitWorkflow.check_in, STAFF)
        assert await handle.result() == "completed"
    assert calls == ["deadline", "check_in", "seen", "complete"]


async def test_nobody_checking_in_makes_the_appointment_a_no_show(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        assert await handle.result() == "no_show"  # the 2-hour timer skips
    assert calls == ["deadline", "no_show"]


async def test_a_deadline_already_past_is_a_no_show_immediately(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, deadline=timedelta(hours=-1)):
        handle = await _start(temporal_env, queue)
        assert await handle.result() == "no_show"
    assert calls == ["deadline", "no_show"]


async def test_an_appointment_that_left_confirmed_ends_the_visit_closed(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, no_show="closed"):
        handle = await _start(temporal_env, queue)
        assert await handle.result() == "closed"
    assert calls == ["deadline", "no_show"]


async def test_a_check_in_that_raced_the_timer_carries_on_as_checked_in(temporal_env):
    """The timer fired but `no_show` found a visit row ("visit_exists"): the check-in won in the
    database and its signal is still on the way. The workflow must not end, and must not
    hang waiting for a check-in signal that was already acted on."""
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, no_show="visit_exists"):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.seen, STAFF)
        await handle.signal(VisitWorkflow.complete, STAFF)
        assert await handle.result() == "completed"
    assert calls == ["deadline", "no_show", "check_in", "seen", "complete"]


async def test_a_cancel_before_check_in_ends_without_writes(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.cancel)
        assert await handle.result() == "cancelled"
    assert calls == ["deadline"]


async def test_a_cancel_midway_stops_before_the_next_step(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.check_in, STAFF)
        await handle.signal(VisitWorkflow.cancel)
        assert await handle.result() == "cancelled"
    assert "seen" not in calls and "complete" not in calls


async def test_a_signal_delivered_twice_acts_once(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        for _ in range(2):
            await handle.signal(VisitWorkflow.check_in, STAFF)
        await handle.signal(VisitWorkflow.seen, STAFF)
        await handle.signal(VisitWorkflow.seen, STAFF)
        await handle.signal(VisitWorkflow.complete, STAFF)
        await handle.signal(VisitWorkflow.complete, STAFF)
        assert await handle.result() == "completed"
    assert calls.count("check_in") == calls.count("seen") == calls.count("complete") == 1
