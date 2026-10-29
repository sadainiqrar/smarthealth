"""The post-commit check-in hint: it reaches a running visit, and the ways it can find
none are not errors -- the check-in it reports is already committed."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio import activity

from app.core.temporal import build_worker
from app.modules.scheduling.contracts import VisitRequest, visit_workflow_id
from app.modules.scheduling.visits import notify_checked_in
from app.modules.scheduling.workflows import VisitWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _fakes(env, calls: list[str]):
    @activity.defn(name="visit.deadline")
    async def deadline(request: VisitRequest) -> str:
        return ((await env.get_current_time()) + timedelta(hours=2)).isoformat()

    @activity.defn(name="visit.no_show")
    async def no_show(request: VisitRequest) -> str:
        calls.append("no_show")
        return "no_show"

    return [deadline, no_show]


def _worker(env, queue, calls):
    return build_worker(env.client, queue, workflows=[VisitWorkflow], activities=_fakes(env, calls))


async def _start_visit(env, appointment_id: uuid.UUID, queue: str):
    return await env.client.start_workflow(
        VisitWorkflow.run, VisitRequest(str(appointment_id)),
        id=visit_workflow_id(appointment_id), task_queue=queue,
    )


async def test_a_running_visit_is_told_and_ends_checked_in(temporal_env):
    calls: list[str] = []
    queue = f"t2-vnote-{uuid.uuid4().hex[:8]}"
    appointment_id = uuid.uuid4()
    async with _worker(temporal_env, queue, calls):
        handle = await _start_visit(temporal_env, appointment_id, queue)
        await notify_checked_in(temporal_env.client, appointment_id)
        assert await handle.result() == "checked_in"
    assert calls == []


async def test_no_visit_workflow_is_not_an_error(temporal_env):
    """A booking confirmed before slice 3 has none -- the documented gap."""
    await notify_checked_in(temporal_env.client, uuid.uuid4())


async def test_a_closed_visit_is_not_an_error_and_is_not_restarted(temporal_env):
    calls: list[str] = []
    queue = f"t2-vnote-{uuid.uuid4().hex[:8]}"
    appointment_id = uuid.uuid4()
    async with _worker(temporal_env, queue, calls):
        handle = await _start_visit(temporal_env, appointment_id, queue)
        await handle.signal(VisitWorkflow.cancel)
        assert await handle.result() == "cancelled"
        run_id = (await handle.describe()).run_id
        await notify_checked_in(temporal_env.client, appointment_id)
        assert (await handle.describe()).run_id == run_id
