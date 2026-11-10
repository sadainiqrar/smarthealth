"""The two workflows Kafka starts in Week 3 slice 4, against the time-skipping server.

Activities are recording fakes with the real names; the workflows are the real ones in the
real sandbox. Neither starts a child, so the shared environment is safe to use.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import PostVisitRequest, ProviderDeactivationRequest
from app.modules.scheduling.workflows import PostVisitWorkflow, ProviderDeactivationWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _worker(env, queue, activities):
    return build_worker(
        env.client, queue, workflows=[PostVisitWorkflow, ProviderDeactivationWorkflow],
        activities=activities,
    )


async def test_post_visit_captures_the_charge(temporal_env):
    charged: list[str] = []

    @activity.defn(name="post_visit.charge")
    async def charge(request: PostVisitRequest) -> str | None:
        charged.append(request.appointment_id)
        return "captured"

    queue = f"post-visit-{uuid.uuid4()}"
    request = PostVisitRequest(visit_id="v1", appointment_id="a1")
    async with _worker(temporal_env, queue, [charge]):
        result = await temporal_env.client.execute_workflow(
            PostVisitWorkflow.run, request, id=f"post-visit-{uuid.uuid4()}", task_queue=queue
        )
    assert result == "captured"
    assert charged == ["a1"]


async def test_post_visit_retries_a_gateway_outage_until_it_charges(temporal_env):
    """The charge is retried without limit (FOLLOW_UP): the visit happened, and giving up
    would mean a visit nobody is billed for. Time skipping makes the backoff free."""
    attempts: list[int] = []

    @activity.defn(name="post_visit.charge")
    async def charge(request: PostVisitRequest) -> str | None:
        attempts.append(activity.info().attempt)
        if len(attempts) < 4:
            raise ApplicationError("gateway unavailable", type="GatewayUnavailable")
        return "captured"

    queue = f"post-visit-{uuid.uuid4()}"
    async with _worker(temporal_env, queue, [charge]):
        result = await temporal_env.client.execute_workflow(
            PostVisitWorkflow.run, PostVisitRequest("v1", "a1"),
            id=f"post-visit-{uuid.uuid4()}", task_queue=queue,
        )
    assert result == "captured" and attempts == [1, 2, 3, 4]


async def test_nothing_to_charge_is_an_ending_not_a_failure(temporal_env):
    @activity.defn(name="post_visit.charge")
    async def charge(request: PostVisitRequest) -> str | None:
        return None

    queue = f"post-visit-{uuid.uuid4()}"
    async with _worker(temporal_env, queue, [charge]):
        result = await temporal_env.client.execute_workflow(
            PostVisitWorkflow.run, PostVisitRequest("v1", "a1"),
            id=f"post-visit-{uuid.uuid4()}", task_queue=queue,
        )
    assert result == "nothing-to-charge"


async def test_deactivation_cancels_each_future_appointment_once(temporal_env):
    """Referenced by tests/cases/prv-001 (a deactivated provider's appointments are cancelled)."""
    cancelled: list[str] = []

    @activity.defn(name="provider.future_appointments")
    async def future(request: ProviderDeactivationRequest) -> list[str]:
        assert request.provider_id == "p1"
        return ["a1", "a2", "a3"]

    @activity.defn(name="provider.cancel_appointment")
    async def cancel(appointment_id: str) -> str:
        if appointment_id == "a2":
            return "skipped"  # the patient checked in meanwhile
        cancelled.append(appointment_id)
        return "cancelled"

    queue = f"deactivation-{uuid.uuid4()}"
    async with _worker(temporal_env, queue, [future, cancel]):
        result = await temporal_env.client.execute_workflow(
            ProviderDeactivationWorkflow.run, ProviderDeactivationRequest("p1"),
            id=f"provider-deactivation-{uuid.uuid4()}", task_queue=queue,
        )
    assert result == "cancelled:2,skipped:1"
    assert cancelled == ["a1", "a3"]


async def test_a_provider_with_nothing_booked_ends_at_once(temporal_env):
    @activity.defn(name="provider.future_appointments")
    async def future(request: ProviderDeactivationRequest) -> list[str]:
        return []

    @activity.defn(name="provider.cancel_appointment")
    async def cancel(appointment_id: str) -> str:
        raise AssertionError("nothing to cancel")

    queue = f"deactivation-{uuid.uuid4()}"
    async with _worker(temporal_env, queue, [future, cancel]):
        result = await temporal_env.client.execute_workflow(
            ProviderDeactivationWorkflow.run, ProviderDeactivationRequest("p1"),
            id=f"provider-deactivation-{uuid.uuid4()}", task_queue=queue,
        )
    assert result == "cancelled:0,skipped:0"


