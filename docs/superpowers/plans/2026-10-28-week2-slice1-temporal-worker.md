# Week 2 Slice 1 — Temporal Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SmartHealth a running Temporal worker — client, registries, worker process, compose service, readiness — proven end to end by a canary workflow, so slices 2–5 only add workflows.

**Architecture:** `app/core/temporal.py` owns client construction, the `WORKFLOWS`/`ACTIVITIES` registries and the canary-based worker check. `app/components.py` is the single place that registers components. `app/worker.py` (`python -m app.worker`) runs every registered component on `Settings.task_queue("scheduling")`; `--check` executes the canary and is the compose healthcheck. The API's `/ready` gains a Temporal server check. A meta-test discovers every `@workflow.defn`/`@activity.defn` by AST, demands it be registered, and validates every registered workflow in Temporal's sandbox.

**Tech Stack:** `temporalio` 1.34 (verified installed and working 2026-10-28; the time-skipping test server downloads and runs offline — a 5-hour workflow sleep completes in 1.5 s), pytest-asyncio 1.4, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §3, §8 row 1.

**Branch:** `feat/temporal-worker`, created from `main` after `docs/week2-design` has been fast-forwarded into it.

---

## Facts the implementer needs (verified, not recalled)

1. **The sandbox re-imports workflow modules.** A module containing a `@workflow.defn` class is imported a second time inside Temporal's sandbox when a `Worker` is built. Anything at module level runs again there. Consequences: a workflow module must have **no import-time side effects** (no registration calls, no `asyncio.run`, no `datetime.now()`), and registration therefore lives in `app/components.py`, which the sandbox never imports.
2. **Offline sandbox validation:** `SandboxedWorkflowRunner().prepare_workflow(_Definition.must_from_class(cls))` raises `RestrictedWorkflowAccessError` for a workflow module that does restricted work at import. `_Definition` is private API (`temporalio.workflow._Definition`); the meta-test uses it knowingly and says so. Constructing a `Replayer` does **not** validate.
3. `Client.connect(..., lazy=True)` exists and does not dial. `client.service_client.check_health()` exists.
4. A `Worker` with zero workflows and zero activities raises at construction — which is one reason the canary exists.
5. pytest-asyncio 1.4 is installed; `asyncio_default_fixture_loop_scope = "function"` in `pyproject.toml`. A session-scoped async fixture therefore needs `loop_scope="session"` on the fixture **and** on the tests that use it.
6. The compose **test** stack publishes Temporal on host port **17233** (`.env.test`); the dev default is 7233.
7. The Stop hook gates `app/core/**`. This slice touches it, so it must add validated cases (Task 9).

## File map

| File | Action | Responsibility |
| --- | --- | --- |
| `pyproject.toml` | modify | add `temporalio` dependency |
| `app/settings.py` | modify | `temporal_host`, `temporal_port`, `temporal_namespace`, `temporal_address` |
| `app/core/temporal_canary.py` | create | `CanaryWorkflow` — sandbox-safe, no side effects |
| `app/core/temporal.py` | create | registries, `TASK_QUEUE`, `connect_client`, `build_worker`, `check_worker` |
| `app/components.py` | create | `register_all()` — the only registration site |
| `app/worker.py` | create | `python -m app.worker [--check]` |
| `app/main.py` | modify | lazy Temporal client on `app.state.temporal` |
| `app/api/health.py` | modify | `_check_temporal` in `/ready` |
| `docker-entrypoint.sh` | modify | exec a given command instead of uvicorn |
| `docker-compose.infra.yml` | modify | Temporal env on `app`; new `worker` service |
| `.env.example` | modify | Temporal settings |
| `tests/tiers/t0_unit/test_settings.py` | modify | settings tests |
| `tests/tiers/t0_unit/test_worker_cli.py` | create | CLI arg parsing |
| `tests/tiers/t1_contract/test_readiness.py` | modify | Temporal in the unreachable set |
| `tests/tiers/t2_workflow/conftest.py` | create | session-scoped time-skipping env |
| `tests/tiers/t2_workflow/test_canary.py` | create | canary runs; check fails with no worker |
| `tests/tiers/t3_integration/conftest.py` | modify | test-stack Temporal port |
| `tests/tiers/t3_integration/test_readiness.py` | modify | Temporal in the healthy set |
| `tests/tiers/t3_integration/test_worker.py` | create | real worker + real server round trip |
| `tests/meta/test_temporal_registry.py` | create | discovery == registration; sandbox validity |
| `tests/cases/sys-008-*.yaml`, `sys-009-*.yaml` | create | Stop-hook cases |
| `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md` | modify | status, commands, harness tally |

---

### Task 0: Branch

- [ ] **Step 1: Land the spec and branch**

```bash
git switch main
git merge --ff-only docs/week2-design
git switch -c feat/temporal-worker
```

Expected: `Switched to a new branch 'feat/temporal-worker'`.

---

### Task 1: Dependency and settings

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Modify: `app/settings.py` (after the Redis block, and a new property)
- Test: `tests/tiers/t0_unit/test_settings.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/tiers/t0_unit/test_settings.py`:

```python
def test_temporal_defaults_point_at_the_dev_stack():
    settings = Settings()
    assert settings.temporal_address == "localhost:7233"
    assert settings.temporal_namespace == "default"


def test_temporal_address_brackets_an_ipv6_host():
    settings = Settings(temporal_host="::1", temporal_port=17233)
    assert settings.temporal_address == "[::1]:17233"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/tiers/t0_unit/test_settings.py -q`
Expected: 2 failures, `AttributeError: 'Settings' object has no attribute 'temporal_address'`.

- [ ] **Step 3: Implement**

In `pyproject.toml`, add to `dependencies` after `"pyjwt>=2.10",`:

```toml
    "temporalio>=1.34,<2",
```

In `app/settings.py`, after the `redis_prefix` line and before `# --- Auth ---`:

```python
    # --- Temporal: durable workflows (PRD D-7) ---
    temporal_host: str = "localhost"
    temporal_port: int = 7233
    temporal_namespace: str = "default"
```

and after the `redis_url` property:

```python
    @property
    def temporal_address(self) -> str:
        """host:port for the Temporal frontend. No scheme: the SDK takes a bare target."""
        return f"{_format_host(self.temporal_host)}:{self.temporal_port}"
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pip install -e ".[dev]" -q && python -m pytest tests/tiers/t0_unit/test_settings.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml app/settings.py tests/tiers/t0_unit/test_settings.py
git commit -m "feat: add Temporal connection settings and the temporalio dependency"
```

(Use a full reasoned body per the repo's convention, ending with the `Co-Authored-By` trailer. Applies to every commit in this plan.)

---

### Task 2: The canary workflow and the Temporal core module

**Files:**
- Create: `app/core/temporal_canary.py`
- Create: `app/core/temporal.py`
- Create: `tests/tiers/t2_workflow/conftest.py`
- Test: `tests/tiers/t2_workflow/test_canary.py`

- [ ] **Step 1: Write the T2 fixture** — `tests/tiers/t2_workflow/conftest.py`:

```python
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
```

- [ ] **Step 2: Write the failing tests** — `tests/tiers/t2_workflow/test_canary.py`:

```python
"""The canary is the worker's readiness probe: a workflow that only a polling,
executing worker can complete. These prove both outcomes the probe distinguishes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

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


async def test_check_worker_fails_when_nothing_polls(temporal_env):
    """No worker on the queue: the canary times out and the probe says so.

    A short timeout on purpose. Verified 2026-10-28: with no worker polling, the
    time-skipping server does NOT skip -- the execution timeout elapses in real time.
    """
    assert await check_worker(temporal_env.client, _queue(), timeout=timedelta(seconds=1)) is False
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/tiers/t2_workflow -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.core.temporal'`.

- [ ] **Step 4: Implement the canary** — `app/core/temporal_canary.py`:

```python
"""The worker's readiness canary.

A workflow with no activities, whose only job is to be completed by a worker that is
polling the task queue. Executing it is the one readiness check that proves a worker
is not merely running but *working*: connected, polling, and able to execute.

This module is imported a second time inside Temporal's workflow sandbox, so it must
stay free of import-time side effects -- no registration here (see app/components.py).
"""

from __future__ import annotations

from temporalio import workflow

CANARY_REPLY = "ok"


@workflow.defn(name="Canary")
class CanaryWorkflow:
    @workflow.run
    async def run(self) -> str:
        return CANARY_REPLY
```

- [ ] **Step 5: Implement the core module** — `app/core/temporal.py`:

```python
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

#: Long enough for a healthy worker under load, short enough to fail a healthcheck
#: before compose's own timeout.
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
```

- [ ] **Step 6: Run to verify pass**

Run: `python -m pytest tests/tiers/t2_workflow -q`
Expected: 3 passed in roughly 3 s (server start plus the 1 s no-worker timeout).

If `test_check_worker_fails_when_nothing_polls` raises an exception type other than the two caught, **do not widen the `except` to `Exception`** — add the specific type the SDK raised and say why in a comment.

- [ ] **Step 7: Run the boundary meta-tests** (the new core modules are framework-free by default)

Run: `python -m pytest tests/meta -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add app/core/temporal.py app/core/temporal_canary.py tests/tiers/t2_workflow
git commit -m "feat: add the Temporal client, component registries and a canary readiness workflow"
```

---

### Task 3: Single registration site and the registry meta-test

**Files:**
- Create: `app/components.py`
- Test: `tests/meta/test_temporal_registry.py`

- [ ] **Step 1: Write the failing meta-test** — `tests/meta/test_temporal_registry.py`:

```python
"""Every Temporal workflow and activity in the source tree is registered, and every
registered workflow is valid inside Temporal's sandbox.

Discovery reads the source with `ast` rather than trusting the registry, because the
failure being guarded against is exactly a definition the registry does not know
about: a workflow no worker runs, whose starts sit in the queue forever.

Sandbox validation catches a workflow module doing non-deterministic work at import
(reading the clock, opening a socket). It cannot see what `run` does at runtime --
the T2 tier covers that, because every T2 test executes inside the same sandbox.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Private API, used knowingly: it is the only offline entry to the validation a Worker
# performs at construction. Verified on temporalio 1.34. If an upgrade removes it,
# build a Worker against the T2 time-skipping environment instead.
from temporalio.workflow import _Definition

from app.components import register_all
from app.core.temporal import ACTIVITIES, WORKFLOWS

pytestmark = pytest.mark.meta

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _is_defn(decorator: ast.expr, namespace: str) -> bool:
    """Matches `@workflow.defn`, `@workflow.defn(...)`, and the activity equivalents."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "defn"
        and isinstance(target.value, ast.Name)
        and target.value.id == namespace
    )


def _discover(namespace: str, node_types: tuple[type, ...]) -> set[str]:
    found: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        module = ".".join(path.relative_to(APP_ROOT.parent).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for node in ast.iter_child_nodes(parent):
                if isinstance(node, node_types) and any(
                    _is_defn(d, namespace) for d in node.decorator_list
                ):
                    owner = f"{parent.name}." if isinstance(parent, ast.ClassDef) else ""
                    found.add(f"{module}.{owner}{node.name}")
    return found


def _qualified(item) -> str:
    target = getattr(item, "__func__", item)  # unwrap bound methods
    return f"{target.__module__}.{target.__qualname__}"


register_all()


def test_every_workflow_definition_is_registered():
    discovered = _discover("workflow", (ast.ClassDef,))
    registered = {_qualified(cls) for cls in WORKFLOWS}
    assert discovered, "found no @workflow.defn at all -- has the layout changed?"
    assert discovered == registered, (
        f"unregistered: {sorted(discovered - registered)}; "
        f"registered but not found in source: {sorted(registered - discovered)}. "
        f"Register workflows in app/components.py."
    )


def test_every_activity_definition_is_registered():
    discovered = _discover("activity", (ast.FunctionDef, ast.AsyncFunctionDef))
    registered = {_qualified(fn) for fn in ACTIVITIES}
    assert discovered == registered, (
        f"unregistered: {sorted(discovered - registered)}; "
        f"registered but not found in source: {sorted(registered - discovered)}. "
        f"Register activities in app/components.py."
    )


def test_register_all_is_idempotent():
    """The worker and the tests may both call it; a second call must not raise."""
    before = (len(WORKFLOWS), len(ACTIVITIES))
    register_all()
    assert (len(WORKFLOWS), len(ACTIVITIES)) == before


@pytest.mark.parametrize("workflow_class", list(WORKFLOWS), ids=lambda cls: cls.__name__)
def test_registered_workflows_pass_the_sandbox(workflow_class):
    SandboxedWorkflowRunner().prepare_workflow(_Definition.must_from_class(workflow_class))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/meta/test_temporal_registry.py -q`
Expected: collection error, `No module named 'app.components'`.

- [ ] **Step 3: Implement** — `app/components.py`:

```python
"""The one place Temporal components are registered.

Not in the workflow modules themselves: Temporal re-imports a workflow's module inside
its sandbox, so a registration call there would run twice. This module is imported by
the worker and the tests, never by the sandbox.

Each slice that adds a workflow or activity adds a line here.
tests/meta/test_temporal_registry.py fails if a definition in the tree is missing.
"""

from __future__ import annotations

from app.core.temporal import WORKFLOWS
from app.core.temporal_canary import CanaryWorkflow

_registered = False


def register_all() -> None:
    """Populate the registries. Idempotent."""
    global _registered
    if _registered:
        return
    WORKFLOWS.register(CanaryWorkflow.__name__, CanaryWorkflow)
    _registered = True
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/meta -q`
Expected: all pass, including `test_registered_workflows_pass_the_sandbox[CanaryWorkflow]`.

- [ ] **Step 5: Prove the sandbox test bites** (do not commit this)

Temporarily add `from datetime import datetime` and `_STARTED = datetime.now()` at module level in `app/core/temporal_canary.py`, run `python -m pytest tests/meta/test_temporal_registry.py -q`, confirm `test_registered_workflows_pass_the_sandbox` fails with `RestrictedWorkflowAccessError`, then `git checkout app/core/temporal_canary.py`.

- [ ] **Step 6: Commit**

```bash
git add app/components.py tests/meta/test_temporal_registry.py
git commit -m "test: require every Temporal definition to be registered and sandbox-valid"
```

---

### Task 4: The worker process

**Files:**
- Create: `app/worker.py`
- Test: `tests/tiers/t0_unit/test_worker_cli.py`

- [ ] **Step 1: Write the failing tests** — `tests/tiers/t0_unit/test_worker_cli.py`:

```python
import pytest

from app import worker

pytestmark = pytest.mark.unit


def test_no_arguments_means_run():
    assert worker.parse_args([]).check is False


def test_check_flag_selects_the_probe():
    assert worker.parse_args(["--check"]).check is True


def test_check_exits_nonzero_when_temporal_is_unreachable(monkeypatch):
    """The compose healthcheck relies on the exit code, so a dead server must fail it."""
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_HOST", "127.0.0.1")
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_PORT", "1")
    assert worker.main(["--check"]) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/tiers/t0_unit/test_worker_cli.py -q`
Expected: `ImportError: cannot import name 'worker' from 'app'`.

- [ ] **Step 3: Implement** — `app/worker.py`:

```python
"""The Temporal worker process.

    python -m app.worker           run every registered workflow and activity
    python -m app.worker --check   exit 0 if a worker executes the canary, else 1

Same codebase as the API, separate process: the two scale independently (PRD R-8).
`--check` is the compose healthcheck. It proves a worker is polling the task queue
and executing -- not that *this* container is, since any worker on the queue may
answer. With one worker per stack that is the same thing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from temporalio.service import RPCError

from app.components import register_all
from app.core.logging import configure_logging
from app.core.temporal import (
    ACTIVITIES,
    TASK_QUEUE,
    WORKFLOWS,
    build_worker,
    check_worker,
    connect_client,
)
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.worker")
    parser.add_argument(
        "--check", action="store_true", help="execute the canary workflow and exit 0/1"
    )
    return parser.parse_args(argv)


async def _run(settings: Settings) -> None:
    register_all()
    client = await connect_client(settings, lazy=False)
    queue = settings.task_queue(TASK_QUEUE)
    logger.info(
        "worker starting",
        extra={"task_queue": queue, "workflows": WORKFLOWS.names(),
               "activities": ACTIVITIES.names()},
    )
    await build_worker(
        client, queue, workflows=list(WORKFLOWS), activities=list(ACTIVITIES)
    ).run()


async def _check(settings: Settings) -> bool:
    try:
        client = await connect_client(settings, lazy=False)
    except (RPCError, RuntimeError) as exc:
        logger.warning("temporal unreachable", extra={"error": str(exc)})
        return False
    return await check_worker(client, settings.task_queue(TASK_QUEUE))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    if args.check:
        return 0 if asyncio.run(_check(settings)) else 1
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/tiers/t0_unit/test_worker_cli.py -q`
Expected: 3 passed.

If `test_check_exits_nonzero_when_temporal_is_unreachable` errors with an exception type other than `RPCError`/`RuntimeError`, add that specific type to the `except` with a comment naming where it came from. Do not catch `Exception`.

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/tiers/t0_unit/test_worker_cli.py
git commit -m "feat: add the worker process and its canary healthcheck"
```

---

### Task 5: API readiness includes Temporal

**Files:**
- Modify: `app/main.py` (lifespan)
- Modify: `app/api/health.py`
- Test: `tests/tiers/t1_contract/test_readiness.py`, `tests/tiers/t3_integration/test_readiness.py`, `tests/tiers/t3_integration/conftest.py`

- [ ] **Step 1: Update the T1 test to expect Temporal** — in `tests/tiers/t1_contract/test_readiness.py`:

Change the loop in `unreachable_infrastructure` to:

```python
    for variable in ("POSTGRES", "MONGO", "REDIS", "TEMPORAL"):
```

and the assertion in `test_ready_reports_503_when_dependencies_are_down` to:

```python
    assert reported == {"postgres": False, "mongo": False, "redis": False, "temporal": False}
```

Add to `test_lifespan_starts_with_no_infrastructure`, after the `session_factory` assertion:

```python
        assert application.state.temporal is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/tiers/t1_contract/test_readiness.py -q`
Expected: 2 failures (no `temporal` key; no `app.state.temporal`).

- [ ] **Step 3: Implement**

In `app/main.py`, add the import `from app.core.temporal import connect_client` and, in `lifespan` after `app.state.redis = ...`:

```python
    # Lazy: the API boots with Temporal down, like every client above. Nothing to
    # close on shutdown -- the SDK client holds no resources until first use.
    app.state.temporal = await connect_client(settings)
```

In `app/api/health.py`, after `_check_redis`:

```python
async def _check_temporal(request: Request) -> DependencyStatus:
    """The server, not the worker: the API needs Temporal to *start* workflows.
    Whether a worker is executing them is the worker's own healthcheck."""
    try:
        await request.app.state.temporal.service_client.check_health()
    except Exception as exc:
        return DependencyStatus(name="temporal", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="temporal", ok=True)
```

and extend the `gather` in `ready`:

```python
        await asyncio.gather(
            guarded(_check_postgres),
            guarded(_check_mongo),
            guarded(_check_redis),
            guarded(_check_temporal),
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/tiers/t1_contract -q`
Expected: all pass.

- [ ] **Step 5: Update T3 to the test stack's Temporal**

In `tests/tiers/t3_integration/conftest.py`, `_test_stack_settings`, add after `redis_prefix=...`:

```python
        temporal_host="localhost",
        temporal_port=int(os.environ.get("SMARTHEALTH_TEST_TEMPORAL_PORT", "17233")),
```

In `tests/tiers/t3_integration/test_readiness.py`, `live_infrastructure`, add:

```python
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_HOST", db_settings.temporal_host)
    monkeypatch.setenv("SMARTHEALTH_TEMPORAL_PORT", str(db_settings.temporal_port))
```

and change the assertion to:

```python
    assert reported == {"postgres": True, "mongo": True, "redis": True, "temporal": True}
```

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/api/health.py tests/tiers/t1_contract/test_readiness.py tests/tiers/t3_integration/conftest.py tests/tiers/t3_integration/test_readiness.py
git commit -m "feat: report Temporal in readiness and hold a lazy client on the app"
```

---

### Task 6: Worker against the real stack (T3)

**Files:**
- Create: `tests/tiers/t3_integration/test_worker.py`

- [ ] **Step 1: Write the test**

```python
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
```

- [ ] **Step 2: Bring the test stack up and run**

Use the `test-stack` skill to bring the stack up (or `docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml up -d --wait`).

Run: `python -m pytest tests/tiers/t3_integration/test_worker.py tests/tiers/t3_integration/test_readiness.py -q`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/tiers/t3_integration/test_worker.py
git commit -m "test: prove the worker answers the canary on the real Temporal server"
```

---

### Task 7: Container topology

**Files:**
- Modify: `docker-entrypoint.sh`
- Modify: `docker-compose.infra.yml`
- Modify: `.env.example`

- [ ] **Step 1: Entrypoint runs a given command** — replace the body of `docker-entrypoint.sh` after `set -euo pipefail` with:

```bash
# With arguments, run them instead of the API -- the worker service passes
# `python -m app.worker`. It skips migrations on purpose: the app container owns them,
# and the worker waits for the app to be healthy before it starts.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Migrating here is right for one replica and wrong for several, which would race.
# Alembic takes a lock so the race is safe rather than corrupting, but replicas would
# serialise on startup. In production this becomes a separate job.
echo "applying migrations..."
python -m alembic upgrade head

echo "starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 2: Compose** — in `docker-compose.infra.yml`:

Add to the `app` service's `environment`:

```yaml
      SMARTHEALTH_TEMPORAL_HOST: temporal
      SMARTHEALTH_TEMPORAL_PORT: 7233
```

Add to the `app` service's `depends_on`:

```yaml
      temporal:
        condition: service_healthy
```

Add a new service directly after `app`:

```yaml
  worker:
    # The Temporal worker: same image as the API, different process (PRD R-8 -- the
    # two scale independently). Waits for `app` to be healthy because the app
    # container applies migrations, and an activity must never meet an unmigrated
    # schema.
    profiles: ["app"]
    build: .
    command: ["python", "-m", "app.worker"]
    environment:
      SMARTHEALTH_ENVIRONMENT: docker
      SMARTHEALTH_POSTGRES_HOST: postgres
      SMARTHEALTH_POSTGRES_PORT: 5432
      SMARTHEALTH_POSTGRES_USER: ${POSTGRES_USER:-smarthealth}
      SMARTHEALTH_POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-smarthealth}
      SMARTHEALTH_POSTGRES_DB: ${POSTGRES_DB:-smarthealth}
      SMARTHEALTH_MONGO_HOST: mongo
      SMARTHEALTH_MONGO_PORT: 27017
      SMARTHEALTH_MONGO_DB: ${MONGO_DB:-smarthealth}
      SMARTHEALTH_TEMPORAL_HOST: temporal
      SMARTHEALTH_TEMPORAL_PORT: 7233
    depends_on:
      app:
        condition: service_healthy
      temporal:
        condition: service_healthy
    healthcheck:
      # Executes the canary workflow: proves a worker is polling AND executing, which
      # "the process is up" does not. Each check leaves one tiny closed workflow in
      # Temporal's history; at this interval that is negligible.
      test: ["CMD", "python", "-m", "app.worker", "--check"]
      interval: 15s
      timeout: 10s
      retries: 8
```

- [ ] **Step 3: `.env.example`** — after the Redis block:

```bash
# --- Temporal (durable workflows) ---
SMARTHEALTH_TEMPORAL_HOST=localhost
SMARTHEALTH_TEMPORAL_PORT=7233
SMARTHEALTH_TEMPORAL_NAMESPACE=default
```

- [ ] **Step 4: Verify the whole system boots**

```bash
docker compose --profile app -f docker-compose.infra.yml up -d --build --wait
docker compose --profile app -f docker-compose.infra.yml ps app worker
curl -s localhost:8000/ready
```

Expected: `app` and `worker` both `healthy`; `/ready` returns `"ready":true` with a `temporal` entry `"ok":true`. `docker compose ... logs worker` shows a JSON `worker starting` line naming `Canary`.

If port 7233 is taken, pass `TEMPORAL_PORT=7234` as `README.md` already documents.

- [ ] **Step 5: Commit**

```bash
git add docker-entrypoint.sh docker-compose.infra.yml .env.example
git commit -m "feat: run the Temporal worker as its own compose service"
```

---

### Task 8: Full verification

- [ ] **Step 1: Fast lane, lint, routing**

```bash
python -m pytest -m "not docker" -q
python -m ruff check app tests
python -m tests.runner.route_check
```

Expected: all pass (count = previous 459 + the new unit, contract, workflow and meta tests); lint clean; routing reports no unsupported or blocked cases.

- [ ] **Step 2: Docker lane**

Run: `python -m pytest -m docker -q`
Expected: all pass (previous 53 + 1).

Record the actual counts — Task 10's docs quote them.

---

### Task 9: Cases for the Stop hook

**Files:**
- Create: `tests/cases/sys-008-every-workflow-is-registered-and-sandbox-safe.yaml`
- Create: `tests/cases/sys-009-a-worker-executes-the-canary-on-the-real-server.yaml`

- [ ] **Step 1: Author both with the `smarthealth-testcase` skill**, from these descriptions:

  - *sys-008* — "every `@workflow.defn` and `@activity.defn` in `app/` is registered in `app/components.py`, and every registered workflow passes Temporal's sandbox validation". Tier `unit` (meta tests run in the fast lane, as `sys-005` does), priority P0, requirement `PART-A-NFR-3` — verify that id exists in `docs/PRD.md` §7 before using it; if the reliability NFR has a different id, use that. `impl: "tests/meta/test_temporal_registry.py::test_every_workflow_definition_is_registered"`.
  - *sys-009* — "a worker on the run's prefixed task queue executes the canary against the compose test stack's Temporal". Tier `integration`, priority P1, requirement `PART-A-OBS-1`. `impl: "tests/tiers/t3_integration/test_worker.py::test_a_worker_on_the_prefixed_queue_answers_the_canary"`.

  Include a comment block in each, in the style of `sys-005`, saying why the case exists.

- [ ] **Step 2: Validate**

Run: `python -m tests.runner.route_check`
Expected: 17 cases, none unsupported or blocked.

- [ ] **Step 3: Commit**

```bash
git add tests/cases/sys-008-*.yaml tests/cases/sys-009-*.yaml tests/cases/CATALOG.md
git commit -m "test: add cases for workflow registration and the worker canary"
```

(`CATALOG.md` only if the skill regenerates it.)

---

### Task 10: Documentation — say exactly what now exists

**Files:** `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`

- [ ] **Step 1: `CLAUDE.md`**
  - In the commands table, add: `| python -m app.worker [--check] | run the Temporal worker; --check executes the canary and exits 0/1 |`.
  - Replace the tally sentence under the harness-requirements table with: requirements **1** (task queues via `Settings.task_queue()` — `app/core/temporal.py`), **3** (registries with a registrant and a discovery meta-test — `app/core/temporal.py`, `tests/meta/test_temporal_registry.py`), **6** and **7** are satisfied; the rest await the code they constrain. Note that requirement 1 is satisfied for Temporal only — Kafka and RabbitMQ names have no callers yet.
  - Update the test-harness row's fast-test count to the number recorded in Task 8.

- [ ] **Step 2: `docs/ARCHITECTURE.md` §3 banner** — replace the opening "Nothing in this section exists in code" note with one that is true: the Temporal client, worker, registries and canary exist (`app/core/temporal.py`, `app/worker.py`); no business workflow, Kafka producer/consumer or Celery task exists yet; `Settings.topic()` and `Settings.queue()` still have zero callers. Add an `app/worker.py` and `app/components.py` entry to §1.4's supporting files.

- [ ] **Step 3: `README.md`** — in "Running it", note that `--profile app` now starts `app` **and** `worker`; add the worker to the architecture diagram's Temporal node label (`Week 2 — worker running; workflows landing`) and remove `temporal` from the `todo` class line only if the diagram's legend defines `todo` as "not started".

- [ ] **Step 4: Verify docs against code** — for every claim added, grep for the file/function named. `CLAUDE.md`'s own rule: grep before asserting.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docs/ARCHITECTURE.md
git commit -m "docs: record the Temporal worker and the harness requirements it satisfies"
```

---

### Task 11: Land

- [ ] **Step 1:** Re-run Task 8 Step 1.
- [ ] **Step 2:** Try `gh pr create`. If it fails with `must be a collaborator`, fast-forward and push:

```bash
git switch main
git merge --ff-only feat/temporal-worker
git push origin main
git branch -d feat/temporal-worker docs/week2-design
```

(Push only with the user's go-ahead in the session.)

---

## What the next plans inherit

Slices 2–5 get their own plans, written once this one lands, because they build on code this one fixes in place. Two decisions are already settled and they must honour them:

- **Activities need a DB session factory and the audit log.** Slice 2 decides how a worker-built resource reaches them. Whatever the choice, registration stays in `app/components.py`, and the registry meta-test must keep passing.
- **Spec correction for slice 2:** rule 4 of `tests/meta/test_import_boundaries.py` (`NEVER_CROSS`) forbids `scheduling/service.py` from importing *any* other module, `patients.public` included. So the D-15 edge is `scheduling.activities → patients.public` and `scheduling.router → patients.public`. `service.py` receives the patient check's result rather than calling it. Two explicit pairs go into `CROSS_MODULE_ALLOWED`. Amend the spec's §3 "Import rules" bullet in slice 2's first commit.
