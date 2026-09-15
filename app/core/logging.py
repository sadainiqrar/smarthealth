"""Structured logging.

JSON lines so that a log aggregator can index fields rather than regex them. The
OpenTelemetry trace/span ids join this record in Week 3.

Installing a formatter on the root logger is not on its own enough to make a *process*
emit structured logs, which is the only claim worth making. uvicorn's `LOGGING_CONFIG`
gives `uvicorn` and `uvicorn.access` their own handlers and sets `propagate = False` on
both, so the one log line that exists per request never reaches root. Until this module
took those loggers over, the running application emitted no JSON at all while four unit
tests correctly reported that the formatter formats.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message"}


class JsonFormatter(logging.Formatter):
    """Renders a log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        return json.dumps(payload, default=str)


#: Loggers that arrive pre-configured with their own handler and `propagate = False`,
#: which puts their records permanently out of reach of anything installed on root.
#: `uvicorn.access` is the one that matters: it carries the single log line most
#: requests produce.
_CAPTURED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _route_to_root(name: str) -> None:
    """Make one third-party logger's records flow to the root handler.

    Detaches its handlers *without* closing them — unlike the JSON handlers below,
    these belong to someone else, and a handler closed here could still be referenced
    by another logger in the same `dictConfig`.

    Setting the level back to `NOTSET` is what makes the configured level mean
    something. A logger's own level decides whether a record is created at all, and
    propagation to root's handlers does not re-check root's level afterwards — so
    leaving `uvicorn.access` pinned at `INFO` would keep access logs flowing even when
    the service is configured to log at `WARNING`.
    """
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger and route uvicorn's logs into it.

    Removes only handlers this function previously installed, never other people's.
    A blanket `handlers.clear()` would also remove pytest's `caplog` capture handler
    — and this runs inside the application lifespan, so it fires on every contract
    test. Idempotent: calling it repeatedly leaves exactly one JSON handler.

    Ordering note: this runs from the application lifespan, which starts *after*
    uvicorn has applied its own `dictConfig`. That is what makes taking the loggers
    over here work regardless of how the process was launched — `uvicorn` directly,
    the container entrypoint, or an ASGI test transport that never configures logging
    at all (where `_route_to_root` finds no handlers and is a no-op).
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]:
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in _CAPTURED_LOGGERS:
        _route_to_root(name)
