"""Temporal plumbing: the client, the component registries, the worker, the canary check.

Framework-free like the rest of app/core: the worker process imports this, and it must
not drag FastAPI in (tests/meta/test_import_boundaries.py).

Every workflow and activity registers in WORKFLOWS / ACTIVITIES (harness requirement
3), and only from app/components.py. tests/meta/test_temporal_registry.py discovers
every definition in the source tree and fails if one is unregistered -- a workflow
nobody registered is a workflow no worker runs, and its starts would hang silently.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.client import Client, WorkflowFailureError
from temporalio.service import RPCError
from temporalio.worker import Worker

from app.core.registry import Registry
from app.core.temporal_canary import CANARY_REPLY, CanaryWorkflow
from app.settings import Settings

#: The one task queue in Week 2. Always pass it through `Settings.task_queue()`, never
#: use it bare: the test isolation layer namespaces queues through that prefix.
TASK_QUEUE = "scheduling"

#: Long enough for a healthy worker under load. This bounds only the canary phase;
#: connecting has its own, much longer, SDK timeout, so `app.worker --check` wraps
#: connect and canary together in `CHECK_TIMEOUT` to stay inside compose's timeout.
CANARY_TIMEOUT = timedelta(seconds=5)

WORKFLOWS: Registry[type] = Registry("workflow")
ACTIVITIES: Registry[Callable[..., Any]] = Registry("activity")


async def connect_client(settings: Settings, *, lazy: bool = True) -> Client:
    """A Temporal client. Lazy by default so the API boots with no Temporal running,
    exactly as every other client in app.main does."""
    return await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace, lazy=lazy
    )


def build_worker(
    client: Client,
    task_queue: str,
    *,
    workflows: Sequence[type] = (),
    activities: Sequence[Callable[..., Any]] = (),
) -> Worker:
    """A worker for the given components. The canary is always included, so every
    worker can answer `check_worker` and a worker is never built empty."""
    all_workflows = list(dict.fromkeys([CanaryWorkflow, *workflows]))
    return Worker(
        client, task_queue=task_queue, workflows=all_workflows, activities=list(activities)
    )


async def check_worker(
    client: Client, task_queue: str, timeout: timedelta = CANARY_TIMEOUT
) -> bool:
    """True only if a worker on `task_queue` executed the canary within `timeout`.

    Catches the two failure families that mean "not ready" -- the workflow timed out
    (nothing polling) or the server refused (not reachable) -- and nothing else, so a
    bug in this function still surfaces as a traceback.
    """
    try:
        result = await client.execute_workflow(
            CanaryWorkflow.run,
            id=f"canary-{uuid.uuid4()}",
            task_queue=task_queue,
            execution_timeout=timeout,
        )
    except (WorkflowFailureError, RPCError):
        return False
    return result == CANARY_REPLY
