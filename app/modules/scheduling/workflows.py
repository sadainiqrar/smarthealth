"""Booking as a durable workflow (spec §4.1, ARCHITECTURE §3.2).

`confirmed` is the last write, so it is unreachable unless every earlier step
succeeded. Any failure from `create_pending` on runs `fail_booking`, which returns a held
slot and marks the appointment failed in one transaction -- partial failure leaves no
orphaned slot and no half-confirmed appointment (PART-A-NFR-2).

Deterministic by construction: no I/O, no clock, no randomness here; all of that lives
in the activities. This module is re-imported inside Temporal's sandbox, so the
activities module (which pulls in SQLAlchemy) is imported through the passthrough.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

from app.modules.scheduling.contracts import BookingRequest, VisitRequest, visit_workflow_id

with workflow.unsafe.imports_passed_through():
    from app.modules.scheduling.activities import SchedulingActivities

#: A step: retried with backoff for transient failure, bounded so a dead database
#: fails the booking (and compensates) instead of hanging it.
STEP = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(
        initial_interval=timedelta(seconds=1), maximum_attempts=5
    ),
}

#: Compensation is retried without limit: giving up would leave exactly the held slot
#: and pending appointment this workflow exists to prevent.
UNLIMITED_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), maximum_interval=timedelta(minutes=1)
)
COMPENSATION = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": UNLIMITED_RETRY}

#: `confirm` is retried without limit too. By the time it runs the slot is claimed and
#: billing and reminders have passed, and its transaction may already have committed
#: when the worker loses the reply (a timeout, a crash after COMMIT). Giving up there
#: would end the workflow failed -- and compensation no-ops on a confirmed row -- so the
#: database would say confirmed while Temporal says failed. `confirm` is idempotent (an
#: already-confirmed row is a no-op), so retrying until it lands is safe. A permanent
#: error (a non-retryable `Conflict`) still ends the booking and compensates.
CONFIRM = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": UNLIMITED_RETRY}


@workflow.defn(name="BookAppointment")
class BookAppointmentWorkflow:
    @workflow.run
    async def run(self, request: BookingRequest) -> str:
        # `create_pending` is inside the try: it may commit and then fail (the reply is
        # lost, or every retry after a committed first attempt times out), leaving a
        # pending row that only compensation would clean up. `fail_booking` no-ops when
        # there is no row, so compensating a create that never committed costs nothing.
        try:
            for step in (
                SchedulingActivities.create_pending,
                SchedulingActivities.validate_booking,
                SchedulingActivities.claim_slot,
                SchedulingActivities.billing_precheck,
                SchedulingActivities.schedule_reminders,
            ):
                await workflow.execute_activity_method(step, request, **STEP)
            # Last, so `confirmed` is unreachable unless every earlier step succeeded.
            await workflow.execute_activity_method(
                SchedulingActivities.confirm, request, **CONFIRM
            )
        except ActivityError as err:
            await workflow.execute_activity_method(
                SchedulingActivities.fail_booking, request, **COMPENSATION
            )
            raise ApplicationError(
                f"booking {request.appointment_id} failed: {err.cause}",
                type="BookingFailed", non_retryable=True,
            ) from err
        # After the try/except, so a failure here can never compensate a confirmed
        # booking. Slice 3 added this command; `patched` keeps bookings that were already
        # in flight when it deployed replaying their original history (spec §8).
        if workflow.patched("booking-starts-visit"):
            try:
                await workflow.start_child_workflow(
                    VisitWorkflow.run, VisitRequest(request.appointment_id),
                    id=visit_workflow_id(request.appointment_id),
                    # The visit outlives the booking: it runs until check-in or deadline.
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    # Never a second run under a closed one: a late child start would
                    # arm a second no-show timer for an appointment already settled.
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                # `visit-<id>` exists, running or closed. Nothing else starts one, so
                # this is defensive: it must never fail a confirmed booking.
                pass
        return "confirmed"


#: Every visit activity. The visit workflow has no compensation to fall back on: a step
#: that gave up would end the workflow failed and leave the appointment with no timer at
#: all, so a short database outage would permanently cost the appointment its no-show. Both
#: activities are idempotent reads or guarded writes, so retrying until the database is
#: back is safe; a domain error (NotFound) is non-retryable and still ends it.
VISIT_STEP = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": UNLIMITED_RETRY}


@workflow.defn(name="Visit")
class VisitWorkflow:
    """The no-show timer for one confirmed appointment (spec D-12, §4.2).

    The database records what staff do -- the endpoints write check-in, seen and complete
    synchronously; this workflow owns only time. It waits until the slot's end plus
    grace, then asks the database (`no_show`, under the appointment lock) whether anyone
    checked in. Signals are hints that let it finish early, not the record: `checked_in`
    may be lost or late (it is sent best-effort after the check-in commits), which is
    why the no-show step settles on the database's answer, not on the flags.

    Returns "checked_in", "cancelled", or the `NoShowOutcome` value from the database:
    "no_show", "visit_exists" (a check-in landed but its signal did not), "closed"."""

    def __init__(self) -> None:
        self._checked_in = False
        self._cancelled = False

    @workflow.signal(name="checked_in")
    def checked_in(self) -> None:
        self._checked_in = True

    @workflow.signal(name="cancel")
    def cancel(self) -> None:
        self._cancelled = True

    def _settled(self) -> bool:
        return self._checked_in or self._cancelled

    @workflow.run
    async def run(self, request: VisitRequest) -> str:
        deadline = datetime.fromisoformat(
            await workflow.execute_activity_method(
                SchedulingActivities.deadline, request, **VISIT_STEP
            )
        )
        remaining = deadline - workflow.now()
        # Only wait while there is time left. `wait_condition(timeout=0)` raises
        # TimeoutError even when the condition already holds -- a check-in signalled
        # before a past deadline would be overridden by a no-show. Past the deadline,
        # the flags (checked below) or the database decide, never a zero-length timer.
        if not self._settled() and remaining > timedelta(0):
            try:
                await workflow.wait_condition(self._settled, timeout=remaining)
            except TimeoutError:
                pass  # the deadline passed with no signal: ask the database
        if self._checked_in:
            return "checked_in"
        if self._cancelled:
            return "cancelled"
        return await workflow.execute_activity_method(
            SchedulingActivities.no_show, request, **VISIT_STEP
        )
