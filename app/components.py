"""The one place Temporal components are registered.

Not in the workflow modules themselves: Temporal re-imports a workflow's module inside
its sandbox, so a registration call there would run twice. This module is imported by
the worker and the tests, never by the sandbox.

Activities are registered as plain functions (discoverable by the registry meta-test)
and bound to instances carrying their resources by `build_activities` at worker start.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.temporal import ACTIVITIES, WORKFLOWS, bind_activities
from app.core.temporal_canary import CanaryWorkflow
from app.events.consumer import CONSUMERS
from app.modules.billing.public import PaymentGateway
from app.modules.notifications.consumers import NOTIFICATION_CONSUMERS
from app.modules.scheduling.consumers import SCHEDULING_CONSUMERS
from app.modules.scheduling.activities import SchedulingActivities
from app.modules.scheduling.workflows import (
    BookAppointmentWorkflow,
    CancelFollowUpWorkflow,
    PostVisitWorkflow,
    ProviderDeactivationWorkflow,
    RescheduleWorkflow,
    VisitWorkflow,
)
from app.settings import Settings, get_settings

_registered = False

_SCHEDULING_ACTIVITIES = (
    SchedulingActivities.create_pending,
    SchedulingActivities.validate_booking,
    SchedulingActivities.claim_slot,
    SchedulingActivities.billing_precheck,
    SchedulingActivities.schedule_reminders,
    SchedulingActivities.confirm,
    SchedulingActivities.fail_booking,
    SchedulingActivities.deadline,
    SchedulingActivities.no_show,
    SchedulingActivities.refund,
    SchedulingActivities.reschedule_complete,
    SchedulingActivities.waitlist_candidates,
    SchedulingActivities.waitlist_fulfil,
    SchedulingActivities.post_visit_charge,
    SchedulingActivities.reminder_due,
    SchedulingActivities.remind,
    SchedulingActivities.provider_future_appointments,
    SchedulingActivities.provider_cancel_appointment,
)


def _activity_name(function: Callable[..., Any]) -> str:
    """The name Temporal routes the activity by (`@activity.defn(name=...)`), so two
    activities that would collide on a worker collide here first, loudly. `__name__`
    would miss exactly that: `confirm` in two classes is two distinct registry keys
    but one Temporal activity. Private API, used knowingly as the meta-test does for
    workflows; verified on temporalio 1.34."""
    return activity._Definition.must_from_callable(function).name


def register_all() -> None:
    """Populate the registries -- workflows, activities and Kafka consumers. Idempotent."""
    global _registered
    if _registered:
        return
    WORKFLOWS.register(CanaryWorkflow.__name__, CanaryWorkflow)
    WORKFLOWS.register(BookAppointmentWorkflow.__name__, BookAppointmentWorkflow)
    WORKFLOWS.register(VisitWorkflow.__name__, VisitWorkflow)
    WORKFLOWS.register(CancelFollowUpWorkflow.__name__, CancelFollowUpWorkflow)
    WORKFLOWS.register(RescheduleWorkflow.__name__, RescheduleWorkflow)
    WORKFLOWS.register(PostVisitWorkflow.__name__, PostVisitWorkflow)
    WORKFLOWS.register(ProviderDeactivationWorkflow.__name__, ProviderDeactivationWorkflow)
    for function in _SCHEDULING_ACTIVITIES:
        ACTIVITIES.register(_activity_name(function), function)
    for spec in (*SCHEDULING_CONSUMERS, *NOTIFICATION_CONSUMERS):
        CONSUMERS.register(spec.name, spec)
    _registered = True


def build_activities(
    session_factory: async_sessionmaker[AsyncSession], audit: AuditLog, clock: Clock,
    no_show_grace: timedelta, *, settings: Settings | None = None,
    gateway: PaymentGateway | None = None,
) -> list[Callable[..., Any]]:
    """Every registered activity, bound to instances holding the worker's resources.

    The payment gateway comes from `settings` unless one is passed (tests point it at
    the test stack's payments-mock, or at a fake)."""
    register_all()
    settings = settings or get_settings()
    gateway = gateway or PaymentGateway(
        settings.payments_url, timeout_seconds=settings.payments_timeout_seconds
    )
    instances = [
        SchedulingActivities(
            session_factory, audit, clock, no_show_grace, gateway=gateway,
            amount_minor=settings.billing_amount_minor, currency=settings.billing_currency,
            reminder_lead=timedelta(hours=settings.reminder_lead_hours),
        )
    ]
    return bind_activities(list(ACTIVITIES), instances)
