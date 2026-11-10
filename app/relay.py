"""The outbox relay process.

    python -m app.relay            publish outbox rows to Kafka until stopped
    python -m app.relay --setup    create topics, register schemas, and exit

Before the loop starts it creates any missing topic and registers every schema, so a
deploy that would break a schema's compatibility fails here, loudly, before anything is
published (spec D-22).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence

from app.core.logging import configure_logging
from app.core.telemetry import instrument_engine, setup_telemetry
from app.db import all_models  # noqa: F401 - the outbox model's foreign metadata
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.events import kafka
from app.events.relay import Relay
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def setup(settings: Settings) -> None:
    created = kafka.ensure_topics(settings)
    ids = kafka.register_schemas(settings)
    logger.info("event backbone ready", extra={"topics_created": created, "schemas": ids})


async def _run(settings: Settings) -> None:
    telemetry = setup_telemetry(settings, "part-a-relay")
    setup(settings)
    engine = create_engine(settings)
    instrument_engine(engine)
    relay = Relay(settings, create_session_factory(engine))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            break
    logger.info("relay starting")
    try:
        await relay.run_forever(stop)
    finally:
        telemetry.flush()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.relay")
    parser.add_argument("--setup", action="store_true", help="create topics and schemas, exit")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    if args.setup:
        setup(settings)
        return 0
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
