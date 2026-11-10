"""Starting the reschedule workflow, and the cancel follow-up's workflow id.

The cancel follow-up is no longer started here. Until Week 3 slice 4 the cancel request
started it best-effort after its commit and lost it if Temporal was down (PRD R-17); now
the cancel's own transaction writes `appointments.cancelled` and the scheduling consumer
starts the follow-up from that event (`consumers.py`), under `cancel_workflow_id`.

A reschedule has written nothing until its workflow runs, so a failed start is a 503 the
client simply retries.

Framework-free, like `booking` and `visits`, so the router stays thin and both policies are
testable against the time-skipping server.
"""

from __future__ import annotations

import hashlib
import json
import logging

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from app.core.errors import ServiceUnavailable, Unprocessable
from app.modules.scheduling.booking import FINGERPRINT_MEMO, started_fingerprint
from app.modules.scheduling.contracts import (
    RescheduleRequest,
    reschedule_workflow_id,
)
from app.modules.scheduling.workflows import RescheduleWorkflow

logger = logging.getLogger(__name__)


def cancel_workflow_id(appointment_id: object) -> str:
    return f"cancel-{appointment_id}"


def reschedule_fingerprint(request: RescheduleRequest) -> str:
    """What the reschedule asks for: which appointment, to which slot, and why. Not the new
    appointment id (derived from the Idempotency-Key, so equal by construction) nor the
    actor (already part of that id). JSON-encoded so no reason text can make two different
    requests encode alike."""
    payload = json.dumps(
        [request.old_appointment_id, request.patient_id, request.new_slot_id, request.reason]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def start_reschedule(
    client: Client, request: RescheduleRequest, *, task_queue: str
) -> str:
    """Start `reschedule-<new_appointment_id>`, or answer for the one already started.

    USE_EXISTING + REJECT_DUPLICATE, exactly as `start_booking` does for a booking: a repeat
    while it runs gets the running workflow, a repeat after it closed raises instead of
    starting a second reschedule. And as there, a repeat must be the same request: the
    fingerprint in the memo turns a key reused for a different reschedule into a 422.
    Temporal unreachable is a 503 -- nothing was written, so the client retries."""
    workflow_id = reschedule_workflow_id(request.new_appointment_id)
    fingerprint = reschedule_fingerprint(request)
    try:
        await client.start_workflow(
            RescheduleWorkflow.run, request, id=workflow_id, task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            memo={FINGERPRINT_MEMO: fingerprint},
        )
    except WorkflowAlreadyStartedError:
        pass
    except (RPCError, RuntimeError) as exc:
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    if await started_fingerprint(client, workflow_id) not in (fingerprint, None):
        raise Unprocessable("Idempotency-Key reused with a different request")
    return workflow_id
