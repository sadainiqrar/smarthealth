"""Duplicate prevention (D-11): a repeated start maps to the same booking, whether the
first is still running or has already finished."""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity

from app.core.temporal import build_worker
from app.modules.scheduling.booking import start_booking
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

NAMES = [
    "create_pending", "validate_booking", "claim_slot", "billing_precheck",
    "schedule_reminders", "confirm", "fail_booking",
]


def _counting_fakes(counter: dict[str, int]):
    def make(name):
        @activity.defn(name=name)
        async def fake(request: BookingRequest) -> None:
            counter[name] = counter.get(name, 0) + 1

        return fake

    return [make(n) for n in NAMES]


async def test_a_repeat_after_completion_does_not_book_again(temporal_env):
    counter: dict[str, int] = {}
    queue = f"t2-start-{uuid.uuid4().hex[:8]}"
    request = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )
    async with build_worker(
        temporal_env.client, queue, workflows=[BookAppointmentWorkflow],
        activities=_counting_fakes(counter),
    ):
        first = await start_booking(temporal_env.client, request, task_queue=queue)
        await temporal_env.client.get_workflow_handle(first).result()
        second = await start_booking(temporal_env.client, request, task_queue=queue)

    assert first == second
    assert counter["create_pending"] == 1
