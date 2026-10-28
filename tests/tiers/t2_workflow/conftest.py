"""Workflow-tier fixtures: one Temporal time-skipping environment per session.

Starting the test server costs about a second; per-test that would dominate the tier.
The convention is `loop_scope="session"` here AND on every test that uses it
(`pytestmark` in each T2 file), so the worker and the environment share one event loop.
It is a convention, not a hard requirement: the 1.34 client is loop-agnostic once
connected, so tests without the mark also pass.

Timers inside a workflow skip forward automatically while a test awaits a result, so
a 15-minute no-show grace period costs nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio.testing import WorkflowEnvironment


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        yield env
    finally:
        await env.shutdown()
