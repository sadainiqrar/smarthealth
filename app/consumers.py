"""The Kafka consumer process: every registered consumer, side by side.

    python -m app.consumers

Same codebase as the API and the worker, separate process (compose service `consumers`),
so a backlog of events never competes with requests for the API's event loop. Each
consumer has its own consumer group (`<kafka_group>.<name>`), so each reads every event
on its topics at its own pace -- that independence is what Kafka is in the stack for.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.components import register_all
from app.core.logging import configure_logging
from app.core.telemetry import instrument_engine, setup_telemetry
from app.core.temporal import connect_client
from app.db import all_models  # noqa: F401 - every table, for the handlers' sessions
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events.consumer import CONSUMERS, ConsumerContext, ConsumerRunner
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def _run(settings: Settings) -> None:
    telemetry = setup_telemetry(settings, "part-a-consumers")
    register_all()
    engine = create_engine(settings)
    instrument_engine(engine)
    factory = create_session_factory(engine)
    # Lazy: a consumer whose handler needs Temporal while it is down waits the outage out
    # (the runtime retries ServiceUnavailable without limit) instead of failing to start.
    context = ConsumerContext(settings=settings, temporal=await connect_client(settings))
    runners = [ConsumerRunner(settings, factory, spec, context=context) for spec in CONSUMERS]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            break
    logger.info("consumers starting", extra={"consumers": CONSUMERS.names()})
    try:
        await asyncio.gather(*(runner.run_forever(stop) for runner in runners))
    finally:
        telemetry.flush()
        await engine.dispose()


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
