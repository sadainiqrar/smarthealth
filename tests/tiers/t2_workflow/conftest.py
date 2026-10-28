"""Workflow-tier fixtures: one Temporal time-skipping environment per session.

Starting the test server costs about a second; per-test that would dominate the tier.
Session scope needs `loop_scope="session"` here AND on every test that uses it
(`pytestmark` in each T2 file), because the project default loop scope is "function"
and a session fixture cannot be awaited from a function-scoped loop.

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
