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
        extra={
            "task_queue": queue,
            "workflows": WORKFLOWS.names(),
            "activities": ACTIVITIES.names(),
        },
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
