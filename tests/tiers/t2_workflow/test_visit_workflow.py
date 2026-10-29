"""The visit workflow is the no-show timer and nothing else (D-12): staff actions are
written by the endpoints, so the workflow waits for the deadline, then lets the database
decide. Activities are recording fakes with the real names; the workflow is the real one,
in the real sandbox.

Every workflow started here finishes inside the worker context (by a signal, or by its
timer): one left on a queue nobody polls stops the time-skipping server from skipping for
the rest of the session.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio import activity

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import VisitRequest
from app.modules.scheduling.state import NoShowOutcome
from app.modules.scheduling.workflows import VisitWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _fakes(
    env, calls: list[str], *, deadline: timedelta = timedelta(hours=2),
    no_show: str = "no_show", transient: int = 0, transient_step: str = "deadline",
):
    """`transient` makes `transient_step` raise a retryable error that many times first."""
    remaining = {"transient": transient}

    def _record(name: str) -> None:
        calls.append(name)
        if name == transient_step and remaining["transient"]:
            remaining["transient"] -= 1
            raise RuntimeError("connection refused")

    @activity.defn(name="visit.deadline")
    async def fake_deadline(request: VisitRequest) -> str:
        _record("deadline")
        # The server's virtual clock, not the wall clock: earlier tests skip time forward,
        # and a wall-clock deadline would already be in the past.
        return ((await env.get_current_time()) + deadline).isoformat()

    @activity.defn(name="visit.no_show")
    async def fake_no_show(request: VisitRequest) -> str:
        _record("no_show")
        return no_show

    return [fake_deadline, fake_no_show]


async def _start(env, queue: str, **kwargs):
    return await env.client.start_workflow(
        VisitWorkflow.run, VisitRequest(appointment_id=str(uuid.uuid4())),
        id=f"visit-{uuid.uuid4()}", task_queue=queue, **kwargs,
    )


def _worker(env, queue, calls, **kwargs):
    return build_worker(
        env.client, queue, workflows=[VisitWorkflow], activities=_fakes(env, calls, **kwargs)
    )


def _queue() -> str:
    return f"t2-visit-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("outcome", [o.value for o in NoShowOutcome])
async def test_with_no_signal_the_timer_fires_and_the_database_decides(temporal_env, outcome):
    """The 2-hour timer skips; `no_show` answers, and its answer is the result -- the
    database, not the workflow's flags, settles whether anyone checked in."""
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, no_show=outcome):
        handle = await _start(temporal_env, queue)
        assert await handle.result() == outcome
    assert calls == ["deadline", "no_show"]


async def test_a_checked_in_signal_ends_the_timer_without_asking(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.checked_in)
        assert await handle.result() == "checked_in"
    assert calls == ["deadline"]


async def test_a_check_in_signalled_before_a_past_deadline_is_still_checked_in(temporal_env):
    """The review's second scenario: the deadline has already passed when the run reads
    it, and the check-in signal is already buffered. `wait_condition(timeout=0)` raises
    TimeoutError even when its condition holds, so a workflow that waited "zero" would
    record this patient as a no-show. Signal-with-start delivers the signal before the
    run starts. Kills: calling `wait_condition` with a zero timeout."""
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, deadline=timedelta(hours=-1)):
        handle = await _start(temporal_env, queue, start_signal="checked_in")
        assert await handle.result() == "checked_in"
    assert "no_show" not in calls


async def test_a_deadline_already_past_goes_straight_to_the_no_show_step(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, deadline=timedelta(hours=-1)):
        handle = await _start(temporal_env, queue)
        assert await handle.result() == "no_show"
    assert calls == ["deadline", "no_show"]


async def test_a_cancel_ends_it_without_asking(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        await handle.signal(VisitWorkflow.cancel)
        assert await handle.result() == "cancelled"
    assert calls == ["deadline"]


async def test_a_signal_delivered_twice_is_harmless(temporal_env):
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls):
        handle = await _start(temporal_env, queue)
        for _ in range(2):
            await handle.signal(VisitWorkflow.checked_in)
        assert await handle.result() == "checked_in"
    assert calls == ["deadline"]


@pytest.mark.parametrize("step", ["deadline", "no_show"])
async def test_a_database_outage_outlasts_the_booking_step_budget(temporal_env, step):
    """The review's third scenario: booking steps give up after 5 attempts, and a visit
    step that did would end the workflow failed -- no timer left, so the appointment is
    never marked no-show. Seven transient failures, then success, must still finish.
    Kills: the visit activities on the bounded `STEP` policy."""
    calls: list[str] = []
    queue = _queue()
    async with _worker(temporal_env, queue, calls, transient=7, transient_step=step):
        handle = await _start(temporal_env, queue)
        assert await handle.result() == "no_show"
    assert calls.count(step) == 8
