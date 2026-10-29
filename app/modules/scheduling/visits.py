"""Telling the visit workflow a check-in happened (D-12, spec §4.2).

The check-in is already committed when this runs: the database is the record, and the
workflow only owns the no-show timer. So the signal is a hint that lets the timer stop
early, sent best-effort -- if it never arrives, the timer fires, `mark_no_show` finds the
visit row under the appointment lock, and the workflow ends "visit_exists". Nothing a
Temporal failure does here can turn a recorded check-in into a no-show, which is why
none of it may fail the request.

Framework-free, like `booking`, so the router stays thin and the policy is testable
against the time-skipping server.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from app.modules.scheduling.contracts import visit_workflow_id
from app.modules.scheduling.workflows import VisitWorkflow

logger = logging.getLogger(__name__)

#: How long a check-in response may wait on this hint. Connecting a lazy client to a
#: blackholed host takes tens of seconds in the SDK; the check-in is already committed,
#: so the request must not wait that out.
NOTIFY_TIMEOUT = timedelta(seconds=5)


async def notify_checked_in(client: Client, appointment_id: uuid.UUID) -> None:
    """Signal `checked_in` to `visit-<appointment_id>`. Never raises.

    NOT_FOUND is expected, not an error: the visit already closed, or it never existed --
    a booking confirmed before slice 3 deployed has no visit workflow. That is a known gap
    (spec §4.2): such appointments are never automatically marked no-show. Any other
    failure (`RPCError`; `RuntimeError` from a lazy client that cannot connect, verified
    on temporalio 1.34; taking longer than `NOTIFY_TIMEOUT`) is logged and dropped, for
    the reason in the module docstring."""
    handle = client.get_workflow_handle(visit_workflow_id(appointment_id))
    try:
        async with asyncio.timeout(NOTIFY_TIMEOUT.total_seconds()):
            await handle.signal(VisitWorkflow.checked_in)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            logger.info(
                "no running visit workflow to notify of check-in",
                extra={"appointment_id": str(appointment_id)},
            )
            return
        logger.warning(
            "could not notify the visit workflow of a check-in; its timer will reconcile",
            extra={"appointment_id": str(appointment_id), "error": str(exc)},
        )
    except (RuntimeError, TimeoutError) as exc:
        logger.warning(
            "could not reach Temporal to notify a check-in; the visit timer will reconcile",
            extra={"appointment_id": str(appointment_id), "error": str(exc)},
        )
