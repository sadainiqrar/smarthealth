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

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from app.modules.scheduling.contracts import BookingRequest

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
        return "confirmed"
