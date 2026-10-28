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
import signal
import sys
from collections.abc import Sequence
from datetime import timedelta

from temporalio.service import RPCError
from temporalio.worker import Worker

from app.components import build_activities, register_all
from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.logging import configure_logging
from app.core.temporal import (
    ACTIVITIES,
    TASK_QUEUE,
    WORKFLOWS,
    build_worker,
    check_worker,
    connect_client,
)
from app.db import all_models  # noqa: F401  - registers every table; see below
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client, get_audit_collection
from app.db.session import create_session_factory
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Overall bound on `--check`, connect included. Connecting to a blackholed host takes
#: tens of seconds in the SDK, which would outlive compose's healthcheck timeout (12 s);
#: staying below it makes the probe fail by its own exit code rather than be killed.
CHECK_TIMEOUT = timedelta(seconds=8)


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
        extra={
            "task_queue": queue,
            "workflows": WORKFLOWS.names(),
            "activities": ACTIVITIES.names(),
        },
    )
    # `all_models` is imported above for its side effect: this process never loads the
    # app's routers, so without it a foreign key to a table whose module nothing imported
    # fails on the first activity's first query.
    # Activities hold real resources: a session factory for Postgres and the audit log's
    # Mongo collection. Built here, disposed in the finally below.
    engine = create_engine(settings)
    mongo = create_mongo_client(settings)
    try:
        audit = AuditLog(get_audit_collection(mongo, settings), Clock())
        activities = build_activities(create_session_factory(engine), audit, Clock())
        worker = build_worker(client, queue, workflows=list(WORKFLOWS), activities=activities)
        await _serve(worker, queue)
    finally:
        await engine.dispose()
        await mongo.close()


async def _serve(worker: Worker, queue: str) -> None:
    loop = asyncio.get_running_loop()
    running = asyncio.ensure_future(worker.run())

    # The loop holds tasks only weakly; an unreferenced shutdown task can be
    # garbage-collected before it finishes draining.
    stopping: set[asyncio.Task[None]] = set()

    def _stop() -> None:
        logger.info("worker stopping", extra={"task_queue": queue})
        # shutdown() waits for completion, so run it as a task; run() then returns.
        # In-flight activities get GRACEFUL_SHUTDOWN (build_worker) to finish before
        # they are cancelled; compose's stop_grace_period leaves room for that.
        task = loop.create_task(worker.shutdown())
        stopping.add(task)
        task.add_done_callback(stopping.discard)

    # The container runs this as PID 1, which ignores SIGTERM unless a handler is
    # installed: without one `docker stop` waits out its grace period and SIGKILLs,
    # and in-flight activities never drain.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # Windows event loops lack add_signal_handler. Local Windows dev stops
            # with Ctrl+C, which surfaces as KeyboardInterrupt and is fine.
            break
    await running


async def _check(settings: Settings) -> bool:
    try:
        return await asyncio.wait_for(_probe(settings), CHECK_TIMEOUT.total_seconds())
    except TimeoutError:
        logger.warning("worker check timed out", extra={"timeout": str(CHECK_TIMEOUT)})
        return False


async def _probe(settings: Settings) -> bool:
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
