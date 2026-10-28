"""Delivering visit actions to the visit workflow (D-12).

Framework-free, like `booking`, so the router stays thin and the policy is testable
against the time-skipping server.
"""

from __future__ import annotations

import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from app.core.errors import Conflict, ServiceUnavailable
from app.modules.scheduling.contracts import VisitRequest, VisitSignal, visit_workflow_id
from app.modules.scheduling.state import VisitAction
from app.modules.scheduling.workflows import VisitWorkflow

__all__ = ["signal_visit", "visit_workflow_id"]


async def signal_visit(
    client: Client, *, appointment_id: uuid.UUID, action: VisitAction,
    actor_user_id: str | None, task_queue: str,
) -> None:
    """Signal-with-start: delivers `action` to the visit workflow, starting it first if it
    is not running -- a booking confirmed before slice 3, or a check-in racing the
    booking's own start. A *closed* visit raises WorkflowAlreadyStartedError (REJECT_
    DUPLICATE), which means the visit already finished: Conflict, not a second visit."""
    try:
        await client.start_workflow(
            VisitWorkflow.run, VisitRequest(appointment_id=str(appointment_id)),
            id=visit_workflow_id(appointment_id), task_queue=task_queue,
            start_signal=action.value,
            start_signal_args=[VisitSignal(actor_user_id=actor_user_id)],
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError as exc:
        raise Conflict(f"the visit for appointment {appointment_id} has already finished") from exc
    except (RPCError, RuntimeError) as exc:
        # RuntimeError: a lazy client raises it ("Failed client connect") when the
        # server is unreachable on first use -- verified on temporalio 1.34.
        raise ServiceUnavailable("the scheduling service is unavailable") from exc
