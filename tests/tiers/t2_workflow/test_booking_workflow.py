"""The headline invariant at the workflow layer: confirmed only after every step, and
any failure compensates. Activities are replaced by recording fakes with the same names;
the workflow under test is the real one, executing in the real sandbox."""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

STEPS = [
    "create_pending", "validate_booking", "claim_slot",
    "billing_precheck", "schedule_reminders", "confirm",
]


def _fakes(calls: list[str], *, fail: str | None = None, transient: int = 0):
    """Activity fakes. `fail` raises a permanent error at that step; `transient` makes
    claim_slot raise a retryable error that many times first."""
    remaining = {"transient": transient}

    def make(name: str):
        @activity.defn(name=name)
        async def fake(request: BookingRequest) -> None:
            calls.append(name)
            if name == "claim_slot" and remaining["transient"]:
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
    queue = f"t2-book-{uuid.uuid4().hex[:8]}"
    async with build_worker(
        env.client, queue, workflows=[BookAppointmentWorkflow], activities=activities
    ):
        return await env.client.execute_workflow(
            BookAppointmentWorkflow.run, _request(),
            id=f"booking-{uuid.uuid4()}", task_queue=queue,
        )


async def test_confirms_only_after_every_step_in_order(temporal_env):
    calls: list[str] = []
    assert await _run(temporal_env, _fakes(calls)) == "confirmed"
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


async def test_a_failed_create_has_nothing_to_compensate(temporal_env):
    calls: list[str] = []
    with pytest.raises(WorkflowFailureError):
        await _run(temporal_env, _fakes(calls, fail="create_pending"))
    assert calls == ["create_pending"]


async def test_a_transient_error_is_retried_not_compensated(temporal_env):
    calls: list[str] = []
    assert await _run(temporal_env, _fakes(calls, transient=2)) == "confirmed"
    assert calls.count("claim_slot") == 3
    assert "fail_booking" not in calls
