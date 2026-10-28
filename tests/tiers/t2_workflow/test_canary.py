"""The canary is the worker's readiness probe: a workflow that only a polling,
executing worker can complete. These prove both outcomes the probe distinguishes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio.testing import WorkflowEnvironment

from app.core.temporal import build_worker, check_worker
from app.core.temporal_canary import CANARY_REPLY, CanaryWorkflow

pytestmark = [pytest.mark.workflow, pytest.mark.asyncio(loop_scope="session")]


def _queue() -> str:
    return f"t2-canary-{uuid.uuid4().hex[:8]}"


async def test_the_canary_completes_on_a_running_worker(temporal_env):
    queue = _queue()
    async with build_worker(temporal_env.client, queue, workflows=[CanaryWorkflow]):
        result = await temporal_env.client.execute_workflow(
            CanaryWorkflow.run, id=f"canary-{uuid.uuid4()}", task_queue=queue
        )
    assert result == CANARY_REPLY


async def test_check_worker_passes_when_a_worker_polls(temporal_env):
    queue = _queue()
    async with build_worker(temporal_env.client, queue, workflows=[CanaryWorkflow]):
        assert await check_worker(temporal_env.client, queue) is True


async def test_check_worker_fails_when_nothing_polls():
    """No worker on the queue: the canary times out and the probe says so.

    A short timeout on purpose. Verified 2026-10-28: with no worker polling, the
    time-skipping server does NOT skip -- the execution timeout elapses in real time.

    Runs in an environment of its own, not the session's. Verified 2026-10-29: a
    workflow left on an unpolled queue leaves the shared time-skipping server unable to
    skip time afterwards, so every later long-timer test (the visit no-show) hangs.
    Sharing the session environment here made test order load-bearing.
    """
    timeout = timedelta(seconds=1)
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        assert await check_worker(env.client, _queue(), timeout=timeout) is False
    finally:
        await env.shutdown()
