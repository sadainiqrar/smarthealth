"""The worker plumbing against the compose test stack's real Temporal server.

T2 proves the logic against the SDK's test server; this proves the configuration --
address, namespace, task-queue prefix -- reaches a real one.
"""

from __future__ import annotations

import pytest

from app.components import register_all
from app.core.temporal import (
    ACTIVITIES,
    TASK_QUEUE,
    WORKFLOWS,
    build_worker,
    check_worker,
    connect_client,
)
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_a_worker_on_the_prefixed_queue_answers_the_canary(db_settings: Settings):
    register_all()
    client = await connect_client(db_settings, lazy=False)
    queue = db_settings.task_queue(TASK_QUEUE)
    assert queue.startswith(db_settings.resource_prefix) and db_settings.resource_prefix

    async with build_worker(
        client, queue, workflows=list(WORKFLOWS), activities=list(ACTIVITIES)
    ):
        assert await check_worker(client, queue) is True
