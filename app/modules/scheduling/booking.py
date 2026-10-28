"""Starting a booking: id derivation and the workflow-start policy (D-10, D-11).

Framework-free so the router stays thin and the policy is testable against the
time-skipping server.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.core.errors import ServiceUnavailable, Unprocessable
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.workflows import BookAppointmentWorkflow

#: Fixed namespace for idempotency-key ids. Never change it: every derived appointment
#: id would change with it, and a client's retry would become a second booking.
IDEMPOTENCY_NAMESPACE = uuid.UUID("6f1d3a52-6a7e-4f5b-9c51-0b1f7d0e2a11")


def appointment_id_for(caller: str, idempotency_key: str | None) -> uuid.UUID:
    """With a key, the same caller and key always name the same appointment; the caller
    is part of the name so two users cannot collide on one key. Without, a new id."""
    if idempotency_key is None:
        return uuid.uuid4()
    return uuid.uuid5(IDEMPOTENCY_NAMESPACE, f"{caller}:{idempotency_key}")


def booking_workflow_id(appointment_id: uuid.UUID | str) -> str:
    return f"booking-{appointment_id}"


#: The workflow memo key holding the request fingerprint.
FINGERPRINT_MEMO = "fingerprint"


def request_fingerprint(request: BookingRequest) -> str:
    """What the booking asks for, as a hash: patient, slot and reason. Not the
    appointment id (derived from the key, so equal by construction) nor the actor
    (already part of the id). JSON-encoded rather than delimiter-joined so no reason
    text can make two different requests encode alike."""
    payload = json.dumps([request.patient_id, request.slot_id, request.reason])
    return hashlib.sha256(payload.encode()).hexdigest()


async def start_booking(client: Client, request: BookingRequest, *, task_queue: str) -> str:
    """Start the booking workflow, or answer for the one already started.

    USE_EXISTING returns the running workflow's handle for a repeat while it runs.
    REJECT_DUPLICATE makes a repeat after it closed raise instead of starting a second
    booking -- Temporal's default would allow it. Either way the caller gets the same
    workflow id back, which is the answer it got the first time.

    A repeat must also be the *same request*. The workflow carries the request's
    fingerprint in its memo, and a key reused with a different body is refused with
    422 (the IETF Idempotency-Key draft's answer) rather than silently answered with
    the first booking -- the client would otherwise believe its second request was
    accepted.
    """
    workflow_id = booking_workflow_id(request.appointment_id)
    fingerprint = request_fingerprint(request)
    try:
        await client.start_workflow(
            BookAppointmentWorkflow.run, request, id=workflow_id, task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            memo={FINGERPRINT_MEMO: fingerprint},
        )
    except WorkflowAlreadyStartedError:
        pass
    except (RPCError, RuntimeError) as exc:
        # RuntimeError: a lazy client raises it ("Failed client connect") when the
        # server is unreachable on first use -- verified on temporalio 1.34.
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    # Checked after every start, not only on WorkflowAlreadyStartedError: under
    # USE_EXISTING a repeat while the first still runs returns normally.
    if await _started_fingerprint(client, workflow_id) not in (fingerprint, None):
        raise Unprocessable("Idempotency-Key reused with a different request")
    return workflow_id


async def _started_fingerprint(client: Client, workflow_id: str) -> str | None:
    """The fingerprint the workflow was started with. None for a workflow started
    before fingerprints existed, which is accepted rather than refused."""
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
        return await description.memo_value(FINGERPRINT_MEMO, None)
    except (RPCError, RuntimeError) as exc:
        raise ServiceUnavailable("the scheduling service is unavailable") from exc


BookingState = Literal["running", "closed", "unknown"]


async def booking_state(client: Client, appointment_id: uuid.UUID) -> BookingState:
    """What Temporal knows of this appointment's booking workflow.

    Asked only when no appointment row exists. "running" is the milliseconds between
    the 202 and the first activity inserting the row. "closed" means the workflow ended
    without ever writing a row -- it failed before `create_pending` committed -- and
    must not be reported as pending, or a poller would wait forever. "unknown" means no
    such booking was ever started. Temporal unreachable is a 503, never an "unknown":
    a 404 would tell the client its booking does not exist when we simply cannot tell.
    """
    try:
        description = await client.get_workflow_handle(
            booking_workflow_id(appointment_id)
        ).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return "unknown"
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    except RuntimeError as exc:
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
    if description.status == WorkflowExecutionStatus.RUNNING:
        return "running"
    return "closed"
