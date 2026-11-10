"""Scheduling's Kafka consumers: domain events that start the workflows following them.

Kafka says what happened; these decide what follows; Temporal does it (spec D-20). Each
handler starts one workflow under a **deterministic id** derived from the event, so a
redelivered event is refused by Temporal (`WorkflowAlreadyStartedError` is success here),
on top of the runtime's own `processed_events` dedupe. None of them steers a running
workflow: human actions stay synchronous database writes (D-12).

- `appointments.cancelled` -> `CancelFollowUpWorkflow` (stop the visit timer, refund, waitlist).
  This closes PRD R-17: the cancel's own transaction wrote the event, so the follow-up can
  no longer be lost the way the best-effort start after the commit could lose it.
- `providers.state` with `is_active = false` -> `ProviderDeactivationWorkflow` (PRD R-25).
  Every such event, not only a transition: the consumer keeps no copy of the previous
  state, and a repeat finds nothing left to cancel.
- `visits.completed` -> `PostVisitWorkflow` (the charge; the follow-up in slice 5).

A scheduling consumer reading `providers.state` is the sanctioned way across the module
boundary (D-8): providers announces, scheduling reacts, neither imports the other.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from app.core.errors import ServiceUnavailable
from app.core.temporal import TASK_QUEUE
from app.events.consumer import ConsumerContext, ConsumerSpec, Event
from app.modules.scheduling.contracts import (
    FollowUpRequest,
    PostVisitRequest,
    ProviderDeactivationRequest,
    post_visit_workflow_id,
    provider_deactivation_workflow_id,
)
from app.modules.scheduling.followups import cancel_workflow_id
from app.modules.scheduling.workflows import (
    CancelFollowUpWorkflow,
    PostVisitWorkflow,
    ProviderDeactivationWorkflow,
)

logger = logging.getLogger(__name__)


async def _start(context: ConsumerContext, run: Any, request: Any, *, workflow_id: str) -> None:
    """Start a workflow once. A duplicate id is the event redelivered: already done.
    Temporal being unreachable is an outage, which the runtime retries without limit."""
    try:
        await context.temporal.start_workflow(
            run, request, id=workflow_id,
            task_queue=context.settings.task_queue(TASK_QUEUE),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        logger.info("workflow already started", extra={"workflow_id": workflow_id})
    except (RPCError, RuntimeError) as exc:
        # RuntimeError: a lazy client that cannot connect (verified on temporalio 1.34).
        raise ServiceUnavailable(f"temporal unavailable: {exc}") from exc


async def on_appointment_cancelled(
    session: AsyncSession, event: Event, context: ConsumerContext
) -> None:
    payload = event.payload
    await _start(
        context, CancelFollowUpWorkflow.run,
        FollowUpRequest(
            appointment_id=payload["appointment_id"], slot_id=payload["slot_id"],
            actor_user_id=payload["initiated_by"],
        ),
        workflow_id=cancel_workflow_id(payload["appointment_id"]),
    )


async def on_provider_state(session: AsyncSession, event: Event, context: ConsumerContext) -> None:
    payload = event.payload
    if payload["is_active"]:
        return
    await _start(
        context, ProviderDeactivationWorkflow.run,
        ProviderDeactivationRequest(provider_id=payload["provider_id"]),
        workflow_id=provider_deactivation_workflow_id(payload["provider_id"], event.event_id),
    )


async def on_visit_completed(session: AsyncSession, event: Event, context: ConsumerContext) -> None:
    payload = event.payload
    await _start(
        context, PostVisitWorkflow.run,
        PostVisitRequest(visit_id=payload["visit_id"], appointment_id=payload["appointment_id"]),
        workflow_id=post_visit_workflow_id(payload["visit_id"]),
    )


_ID = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"

#: The consumers, with one fixture event each for the idempotency meta-test.
SCHEDULING_CONSUMERS: tuple[ConsumerSpec, ...] = (
    ConsumerSpec(
        name="scheduling.cancel-follow-up", topics=("appointments.cancelled",),
        handler=on_appointment_cancelled,
        fixtures=({"appointment_id": _ID, "patient_id": _ID, "provider_id": _ID,
                   "slot_id": _ID, "starts_at": None, "reason": "r", "initiated_by": None},),
    ),
    ConsumerSpec(
        name="scheduling.provider-deactivation", topics=("providers.state",),
        handler=on_provider_state,
        fixtures=({"provider_id": _ID, "first_name": "A", "last_name": "B", "specialty": "GP",
                   "license_number": "L1", "is_active": False, "department_ids": []},),
    ),
    ConsumerSpec(
        name="scheduling.post-visit", topics=("visits.completed",),
        handler=on_visit_completed,
        fixtures=({"visit_id": _ID, "appointment_id": _ID, "patient_id": _ID,
                   "provider_id": _ID, "clinic_id": _ID, "department_id": None,
                   "checked_in_at": None, "seen_at": None, "completed_at": None},),
    ),
)
