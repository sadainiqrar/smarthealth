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

from temporalio.client import Client, Interceptor, WorkflowFailureError
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.runtime import OpenTelemetryConfig, Runtime, TelemetryConfig
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

#: How long `worker.shutdown()` lets in-flight activities finish before cancelling them.
#: The SDK default is zero, i.e. cancel immediately, so without this nothing drains.
#: 8 s is long enough for a typical activity to complete after SIGTERM and must stay
#: below the `worker` service's `stop_grace_period` in docker-compose.infra.yml (15 s),
#: or `docker stop` SIGKILLs the process mid-drain.
GRACEFUL_SHUTDOWN = timedelta(seconds=8)

WORKFLOWS: Registry[type] = Registry("workflow")
ACTIVITIES: Registry[Callable[..., Any]] = Registry("activity")


#: How often Temporal core pushes its SDK metrics; matches the application's interval.
TEMPORAL_METRIC_PERIOD = timedelta(seconds=10)


def tracing_interceptors() -> list[Interceptor]:
    """The interceptors every client gets: OpenTelemetry tracing.

    One interceptor serves both sides. On a client it opens `StartWorkflow:<type>` spans
    and puts the trace context in the workflow's headers; a Worker built from that client
    inherits it and opens `RunWorkflow:<type>` / `RunActivity:<type>` spans from those
    headers -- so a booking's activities join the trace of the request that started it.
    It uses the global tracer provider (app/core/telemetry.py).
    """
    return [TracingInterceptor()]


def telemetry_runtime(settings: Settings) -> Runtime | None:
    """A Temporal runtime exporting the SDK's own metrics over OTLP/gRPC, or None.

    These come from the SDK's Rust core, not from Python, which is why they need their
    own exporter: workflow and activity outcomes, schedule-to-start latency, task-queue
    polling. None (no endpoint configured) keeps the SDK's default runtime.
    """
    if not settings.otel_endpoint_grpc:
        return None
    return Runtime(
        telemetry=TelemetryConfig(
            metrics=OpenTelemetryConfig(
                url=settings.otel_endpoint_grpc, metric_periodicity=TEMPORAL_METRIC_PERIOD
            )
        )
    )


async def connect_client(
    settings: Settings, *, lazy: bool = True, runtime: Runtime | None = None
) -> Client:
    """A Temporal client, traced. Lazy by default so the API boots with no Temporal
    running, exactly as every other client in app.main does."""
    extra: dict[str, Any] = {"runtime": runtime} if runtime is not None else {}
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        lazy=lazy,
        interceptors=tracing_interceptors(),
        **extra,
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
        client,
        task_queue=task_queue,
        workflows=all_workflows,
        activities=list(activities),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN,
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


def bind_activities(
    functions: Sequence[Callable[..., Any]], instances: Sequence[object]
) -> list[Callable[..., Any]]:
    """Turn registered activity functions into what a Worker runs.

    The registry holds plain functions -- discoverable, and comparable with the source
    tree by the registry meta-test. Activities that need a database are methods of a
    class constructed with its resources at worker start-up, so each method is bound to
    the instance of its owning class here. A method with no instance is a wiring bug and
    raises, rather than registering an activity that fails on its first call.
    """
    by_class = {type(instance).__qualname__: instance for instance in instances}
    bound: list[Callable[..., Any]] = []
    for function in functions:
        owner, _, name = function.__qualname__.rpartition(".")
        if not owner:
            bound.append(function)
            continue
        if owner not in by_class:
            raise LookupError(f"no instance of {owner} to bind activity {name} to")
        bound.append(getattr(by_class[owner], name))
    return bound
