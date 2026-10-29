"""The headline invariant at the workflow layer: confirmed only after every step, and
any failure compensates. Activities are replaced by recording fakes with the same names;
the workflow under test is the real one, executing in the real sandbox."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import BookingRequest, VisitRequest, visit_workflow_id
from app.modules.scheduling.workflows import BookAppointmentWorkflow, VisitWorkflow
from tests.tiers.t2_workflow.visit_fakes import finish_visit, open_visit_fakes

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

STEPS = [
    "create_pending", "validate_booking", "claim_slot",
    "billing_precheck", "schedule_reminders", "confirm",
]


def _fakes(
    calls: list[str], *, fail: str | None = None, transient: int = 0,
    transient_step: str = "claim_slot",
):
    """Activity fakes. `fail` raises a permanent error at that step; `transient` makes
    `transient_step` raise a retryable error that many times first."""
    remaining = {"transient": transient}

    def make(name: str):
        @activity.defn(name=f"booking.{name}")
        async def fake(request: BookingRequest) -> None:
            calls.append(name)
            if name == transient_step and remaining["transient"]:
                remaining["transient"] -= 1
                raise RuntimeError("connection reset")
            if name == fail:
                raise ApplicationError(f"{name} refused", type="Conflict", non_retryable=True)

        return fake

    return [make(name) for name in [*STEPS, "fail_booking"]]


def _request() -> BookingRequest:
    return BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )


async def _run(env, activities) -> str:
    request = _request()
    release = asyncio.Event()
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*activities, *open_visit_fakes(env, release)],
    ):
        result = await env.client.execute_workflow(
            BookAppointmentWorkflow.run, request,
            id=f"booking-{uuid.uuid4()}", task_queue=queue,
        )
        # A confirmed booking started a child visit; let it end inside the worker.
        await finish_visit(env, request.appointment_id, release)
        return result


async def test_confirms_only_after_every_step_in_order(isolated_env):
    calls: list[str] = []
    assert await _run(isolated_env, _fakes(calls)) == "confirmed"
    assert calls == STEPS


@pytest.mark.parametrize("failing", STEPS[1:])
async def test_any_failing_step_compensates_and_never_confirms(temporal_env, failing):
    calls: list[str] = []
    with pytest.raises(WorkflowFailureError):
        await _run(temporal_env, _fakes(calls, fail=failing))
    assert calls[-1] == "fail_booking"
    assert calls.count(failing) == 1, "a permanent failure must not be retried"
    if failing != "confirm":
        assert "confirm" not in calls


async def test_a_failed_create_is_compensated_too(temporal_env):
    """create_pending may have committed before it failed (a lost reply), so its failure
    compensates like any other; fail_booking no-ops when there is no row."""
    calls: list[str] = []
    with pytest.raises(WorkflowFailureError):
        await _run(temporal_env, _fakes(calls, fail="create_pending"))
    assert calls == ["create_pending", "fail_booking"]


async def test_a_transient_error_is_retried_not_compensated(isolated_env):
    calls: list[str] = []
    assert await _run(isolated_env, _fakes(calls, transient=2)) == "confirmed"
    assert calls.count("claim_slot") == 3
    assert "fail_booking" not in calls


async def test_confirm_outlasts_the_step_retry_budget(isolated_env):
    """Ordinary steps give up after 5 attempts; confirm must not. A transient failure
    there could follow a committed confirm, and ending the workflow failed would leave
    Temporal saying failed while the database says confirmed."""
    calls: list[str] = []
    activities = _fakes(calls, transient=7, transient_step="confirm")
    assert await _run(isolated_env, activities) == "confirmed"
    assert calls.count("confirm") == 8
    assert "fail_booking" not in calls


async def test_a_confirmed_booking_starts_the_visit(isolated_env):
    request = _request()
    release = asyncio.Event()
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        isolated_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*_fakes([]), *open_visit_fakes(isolated_env, release)],
    ):
        result = await isolated_env.client.execute_workflow(
            BookAppointmentWorkflow.run, request,
            id=f"booking-{uuid.uuid4()}", task_queue=queue,
        )
        visit = isolated_env.client.get_workflow_handle(visit_workflow_id(request.appointment_id))
        # The booking has completed, yet the visit outlives it: the child is abandoned.
        assert result == "confirmed"
        assert (await visit.describe()).status == WorkflowExecutionStatus.RUNNING
        await visit.signal(VisitWorkflow.cancel)
        release.set()
        assert await visit.result() == "cancelled"


async def test_a_visit_already_started_does_not_fail_the_booking(isolated_env):
    """If `visit-<id>` already exists when the booking's child start runs, the start
    finds the id taken; that must not undo a confirmed booking. Nothing in the app starts
    a visit other than booking, so this is the defensive path."""
    request = _request()
    release = asyncio.Event()
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    calls: list[str] = []
    async with build_worker(
        isolated_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*_fakes(calls), *open_visit_fakes(isolated_env, release)],
    ):
        # Parked in the held `deadline` fake, so the time-skipping server cannot fire
        # its timer while the booking below runs.
        visit = await isolated_env.client.start_workflow(
            VisitWorkflow.run, VisitRequest(request.appointment_id),
            id=visit_workflow_id(request.appointment_id), task_queue=queue,
        )
        result = await isolated_env.client.execute_workflow(
            BookAppointmentWorkflow.run, request,
            id=f"booking-{uuid.uuid4()}", task_queue=queue,
        )
        assert result == "confirmed"
        assert "fail_booking" not in calls
        await visit.signal(VisitWorkflow.cancel)
        release.set()
        assert await visit.result() == "cancelled"
        assert calls.count("confirm") == 1


async def test_a_closed_visit_is_not_started_again_by_a_late_booking(isolated_env):
    """A visit run that already closed for this appointment (one started before this
    design, say, and cancelled) must not be restarted by a booking whose child start runs
    late: a second run would arm a second no-show timer for the same appointment. The
    child start uses REJECT_DUPLICATE, so it fails, and the booking still confirms. Kills:
    the reuse policy left at its ALLOW_DUPLICATE default."""
    request = _request()
    release = asyncio.Event()
    release.set()
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        isolated_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*_fakes([]), *open_visit_fakes(isolated_env, release)],
    ):
        visit = await isolated_env.client.start_workflow(
            VisitWorkflow.run, VisitRequest(request.appointment_id),
            id=visit_workflow_id(request.appointment_id), task_queue=queue,
        )
        await visit.signal(VisitWorkflow.cancel)
        assert await visit.result() == "cancelled"
        first_run = (await visit.describe()).run_id

        result = await isolated_env.client.execute_workflow(
            BookAppointmentWorkflow.run, request,
            id=f"booking-{uuid.uuid4()}", task_queue=queue,
        )
        assert result == "confirmed"
        latest = await isolated_env.client.get_workflow_handle(
            visit_workflow_id(request.appointment_id)
        ).describe()
        assert latest.run_id == first_run
        assert latest.status == WorkflowExecutionStatus.COMPLETED


async def test_a_failed_booking_starts_no_visit(temporal_env):
    request = _request()
    release = asyncio.Event()
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        temporal_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*_fakes([], fail="claim_slot"), *open_visit_fakes(temporal_env, release)],
    ):
        with pytest.raises(WorkflowFailureError):
            await temporal_env.client.execute_workflow(
                BookAppointmentWorkflow.run, request,
                id=f"booking-{uuid.uuid4()}", task_queue=queue,
            )
        with pytest.raises(RPCError, match="not found"):
            await temporal_env.client.get_workflow_handle(
                visit_workflow_id(request.appointment_id)
            ).describe()
