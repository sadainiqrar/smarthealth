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


@pytest_asyncio.fixture(loop_scope="session")
async def isolated_env() -> AsyncIterator[WorkflowEnvironment]:
    """A private time-skipping environment, for tests whose workflow starts a child.

    On temporalio 1.34's test server, a workflow that starts a child and closes (even
    with the child already finished, ABANDON or not) leaves time skipping disabled for
    the rest of that server's life -- every later timer or retry backoff in the session
    then waits in real time. Reproduced outside this repo with a bare parent/child pair.
    A booking that confirms starts a child visit, so those tests get their own server
    rather than poisoning the shared one. Production Temporal is not affected.
    """
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        yield env
    finally:
        await env.shutdown()
