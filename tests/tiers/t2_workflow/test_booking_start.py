"""Duplicate prevention (D-11): a repeated start maps to the same booking, whether the
first is still running or has already finished."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid

import pytest
from temporalio import activity

from app.core.errors import Unprocessable
from app.core.temporal import build_worker
from app.modules.scheduling.booking import start_booking
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow, VisitWorkflow
from tests.tiers.t2_workflow.visit_fakes import finish_visit, open_visit_fakes

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]

NAMES = [
    "create_pending", "validate_booking", "claim_slot", "billing_precheck",
    "schedule_reminders", "confirm", "fail_booking",
]


def _counting_fakes(counter: dict[str, int]):
    def make(name):
        @activity.defn(name=f"booking.{name}")
        async def fake(request: BookingRequest) -> None:
            counter[name] = counter.get(name, 0) + 1

        return fake

    return [make(n) for n in NAMES]


async def test_a_repeat_after_completion_does_not_book_again(isolated_env):
    counter: dict[str, int] = {}
    queue = f"t2-start-{uuid.uuid4().hex[:8]}"
    release = asyncio.Event()
    request = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )
    async with build_worker(
        isolated_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*_counting_fakes(counter), *open_visit_fakes(isolated_env, release)],
    ):
        first = await start_booking(isolated_env.client, request, task_queue=queue)
        await isolated_env.client.get_workflow_handle(first).result()
        first_run = (await isolated_env.client.get_workflow_handle(first).describe()).run_id
        second = await start_booking(isolated_env.client, request, task_queue=queue)
        # Inside the worker context, so that if the repeat *had* started a second run it
        # would execute here and count a second create_pending. An unpinned handle
        # follows the latest run, which is the second one if it exists.
        latest = isolated_env.client.get_workflow_handle(second)
        await latest.result()
        assert (await latest.describe()).run_id == first_run
        await finish_visit(isolated_env, request.appointment_id, release)

    assert first == second
    assert counter["create_pending"] == 1


async def test_a_key_reused_for_a_different_request_is_refused(isolated_env):
    """Same appointment id (same caller and key), different slot: Temporal would answer
    with the first booking; the fingerprint in the memo turns that into a refusal."""
    counter: dict[str, int] = {}
    queue = f"t2-start-{uuid.uuid4().hex[:8]}"
    release = asyncio.Event()
    request = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )
    other_slot = dataclasses.replace(request, slot_id=str(uuid.uuid4()))
    async with build_worker(
        isolated_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[*_counting_fakes(counter), *open_visit_fakes(isolated_env, release)],
    ):
        first = await start_booking(isolated_env.client, request, task_queue=queue)
        await isolated_env.client.get_workflow_handle(first).result()
        with pytest.raises(Unprocessable, match="Idempotency-Key"):
            await start_booking(isolated_env.client, other_slot, task_queue=queue)
        # The same request is still answered.
        assert await start_booking(isolated_env.client, request, task_queue=queue) == first
        await finish_visit(isolated_env, request.appointment_id, release)


async def test_a_key_reused_while_the_first_still_runs_is_refused(isolated_env):
    """Under USE_EXISTING a repeat against a *running* workflow returns normally rather
    than raising, so the fingerprint must be checked after every start. The first
    booking is held running by a create_pending that waits to be released -- not by
    leaving its queue unpolled, which leaves state behind that stalls the time-skipping
    server for later tests."""
    queue = f"t2-start-{uuid.uuid4().hex[:8]}"
    release = asyncio.Event()
    entered, release = asyncio.Event(), asyncio.Event()

    @activity.defn(name="booking.create_pending")
    async def held_create(request: BookingRequest) -> None:
        entered.set()
        await release.wait()

    others = _counting_fakes({})[1:]  # NAMES[0] is create_pending, replaced above
    request = BookingRequest(
        appointment_id=str(uuid.uuid4()), patient_id=str(uuid.uuid4()),
        slot_id=str(uuid.uuid4()), actor_user_id=None,
    )
    async with build_worker(
        isolated_env.client, queue, workflows=[BookAppointmentWorkflow, VisitWorkflow],
        activities=[held_create, *others, *open_visit_fakes(isolated_env, release)],
    ):
        first = await start_booking(isolated_env.client, request, task_queue=queue)
        await entered.wait()
        try:
            with pytest.raises(Unprocessable):
                await start_booking(
                    isolated_env.client, dataclasses.replace(request, reason="different"),
                    task_queue=queue,
                )
        finally:
            release.set()
        assert await isolated_env.client.get_workflow_handle(first).result() == "confirmed"
        await finish_visit(isolated_env, request.appointment_id, release)
