"""Signal-with-start: a visit action reaches the workflow whether or not it is running,
and a closed visit is a conflict rather than a second visit."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio import activity

from app.core.errors import Conflict
from app.core.temporal import build_worker
from app.modules.scheduling.contracts import VisitRequest, VisitSignal, visit_workflow_id
from app.modules.scheduling.state import VisitAction
from app.modules.scheduling.visits import signal_visit
from app.modules.scheduling.visits import visit_workflow_id as reexported
from app.modules.scheduling.workflows import VisitWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _fakes(env, calls: list[tuple[str, str | None]]):
    @activity.defn(name="visit.deadline")
    async def deadline(request: VisitRequest) -> str:
        return ((await env.get_current_time()) + timedelta(hours=2)).isoformat()

    @activity.defn(name="visit.no_show")
    async def no_show(request: VisitRequest) -> bool:
        calls.append(("no_show", None))
        return True

    def two_arg(name: str):
        @activity.defn(name=f"visit.{name}")
        async def fake(request: VisitRequest, signal: VisitSignal) -> None:
            calls.append((name, signal.actor_user_id))

        return fake

    return [deadline, no_show, *(two_arg(n) for n in ("check_in", "seen", "complete"))]


def _worker(env, queue, calls):
    return build_worker(env.client, queue, workflows=[VisitWorkflow], activities=_fakes(env, calls))


async def _signal(env, appointment_id, action, queue, actor="staff-1"):
    await signal_visit(
        env.client, appointment_id=appointment_id, action=action, actor_user_id=actor,
        task_queue=queue,
    )


async def test_the_workflow_id_helper_is_shared():
    assert reexported is visit_workflow_id
    assert visit_workflow_id("abc") == "visit-abc"


async def test_signalling_an_absent_visit_starts_it_and_delivers_the_signal(temporal_env):
    calls: list[tuple[str, str | None]] = []
    queue = f"t2-vsig-{uuid.uuid4().hex[:8]}"
    appointment_id = uuid.uuid4()
    async with _worker(temporal_env, queue, calls):
        await _signal(temporal_env, appointment_id, VisitAction.CHECK_IN, queue)
        handle = temporal_env.client.get_workflow_handle(visit_workflow_id(appointment_id))
        await handle.signal(VisitWorkflow.seen, VisitSignal(actor_user_id="staff-1"))
        await handle.signal(VisitWorkflow.complete, VisitSignal(actor_user_id="staff-1"))
        assert await handle.result() == "completed"
    assert ("check_in", "staff-1") in calls


async def test_signalling_a_running_visit_delivers_without_a_second_start(temporal_env):
    calls: list[tuple[str, str | None]] = []
    queue = f"t2-vsig-{uuid.uuid4().hex[:8]}"
    appointment_id = uuid.uuid4()
    async with _worker(temporal_env, queue, calls):
        for action in (VisitAction.CHECK_IN, VisitAction.SEEN, VisitAction.COMPLETE):
            await _signal(temporal_env, appointment_id, action, queue)
        handle = temporal_env.client.get_workflow_handle(visit_workflow_id(appointment_id))
        assert await handle.result() == "completed"
    assert [name for name, _ in calls] == ["check_in", "seen", "complete"]


async def test_signalling_a_closed_visit_is_a_conflict_not_a_second_visit(temporal_env):
    calls: list[tuple[str, str | None]] = []
    queue = f"t2-vsig-{uuid.uuid4().hex[:8]}"
    appointment_id = uuid.uuid4()
    async with _worker(temporal_env, queue, calls):
        for action in (VisitAction.CHECK_IN, VisitAction.SEEN, VisitAction.COMPLETE):
            await _signal(temporal_env, appointment_id, action, queue)
        handle = temporal_env.client.get_workflow_handle(visit_workflow_id(appointment_id))
        await handle.result()
        with pytest.raises(Conflict, match="already finished"):
            await _signal(temporal_env, appointment_id, VisitAction.CHECK_IN, queue)
    assert [name for name, _ in calls].count("check_in") == 1
