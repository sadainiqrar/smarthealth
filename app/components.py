"""The one place Temporal components are registered.

Not in the workflow modules themselves: Temporal re-imports a workflow's module inside
its sandbox, so a registration call there would run twice. This module is imported by
the worker and the tests, never by the sandbox.

Activities are registered as plain functions (discoverable by the registry meta-test)
and bound to instances carrying their resources by `build_activities` at worker start.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import ACTIVITIES, WORKFLOWS, bind_activities
from app.core.temporal_canary import CanaryWorkflow
from app.modules.scheduling.activities import SchedulingActivities

_registered = False

_SCHEDULING_ACTIVITIES = (
    SchedulingActivities.create_pending,
    SchedulingActivities.validate_booking,
    SchedulingActivities.claim_slot,
    SchedulingActivities.billing_precheck,
    SchedulingActivities.schedule_reminders,
    SchedulingActivities.confirm,
    SchedulingActivities.fail_booking,
)


def register_all() -> None:
    """Populate the registries. Idempotent."""
    global _registered
    if _registered:
        return
    WORKFLOWS.register(CanaryWorkflow.__name__, CanaryWorkflow)
    for function in _SCHEDULING_ACTIVITIES:
        ACTIVITIES.register(function.__name__, function)
    _registered = True


def build_activities(
    session_factory: async_sessionmaker[AsyncSession], audit: AuditLog, clock: Clock
) -> list[Callable[..., Any]]:
    """Every registered activity, bound to instances holding the worker's resources."""
    register_all()
    instances = [SchedulingActivities(session_factory, audit, clock)]
    return bind_activities(list(ACTIVITIES), instances)
