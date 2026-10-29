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
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

from app.modules.scheduling.contracts import (
    BookingRequest,
    FollowUpRequest,
    RescheduleRequest,
    VisitRequest,
    booking_workflow_id,
    visit_workflow_id,
)

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


#: Every follow-up activity (refund, reschedule completion, waitlist). They are things that
#: must eventually happen and each is idempotent, so they retry until the database is back
#: rather than ending the workflow and losing the follow-up; a domain error is
#: non-retryable and still ends it.
FOLLOW_UP = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": UNLIMITED_RETRY}


async def _stop_visit(appointment_id: str) -> None:
    """Tell the visit timer its appointment is gone. Best-effort: if this is lost the timer
    still ends by itself at the deadline, when `no_show` finds the appointment no longer
    confirmed and answers "closed"."""
    try:
        await workflow.get_external_workflow_handle(visit_workflow_id(appointment_id)).signal(
            "cancel"
        )
    except ApplicationError:
        pass  # no visit workflow (a booking from before slice 3, or it already finished)


async def _promote_waitlist(slot_id: str) -> str | None:
    """Offer a released slot to waitlisted patients, oldest first; return the id of the
    appointment that confirmed, or None.

    Each attempt is a child booking with the id `booking-wl-<entry>`. With
    ALLOW_DUPLICATE_FAILED_ONLY a failed attempt may be retried by a later release, while
    a confirmed one can never run twice and a running one cannot be started again -- so
    two releases racing for the same entry cannot double-book it. `candidates` is a plain
    read; this id, not a lock, is the guard.

    The appointment id is `workflow.uuid4()`: deterministic, so a replay derives the same
    id, but a random (v4) UUID rather than a time-ordered v7 -- a deliberate exception to
    PART-A-NFR-5, since nothing time-based is replay-safe inside a workflow but
    `workflow.now()`, and a hand-built v7 from it would add code to save index locality on
    a rare write."""
    candidates = await workflow.execute_activity_method(
        SchedulingActivities.waitlist_candidates, slot_id, **FOLLOW_UP
    )
    for entry_id, patient_id in candidates:
        appointment_id = str(workflow.uuid4())
        try:
            await workflow.execute_child_workflow(
                BookAppointmentWorkflow.run,
                BookingRequest(
                    appointment_id, patient_id, slot_id, actor_user_id="system:waitlist"
                ),
                id=f"booking-wl-{entry_id}",
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                # ABANDON: a booking compensates for itself. Terminating this parent must
                # not kill the child between its claim and its compensation, leaving the
                # slot held and the appointment pending (D-10's orphan).
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except (WorkflowAlreadyStartedError, ChildWorkflowError):
            continue  # already promoted or being promoted, or this booking failed: next
        try:
            await workflow.execute_activity_method(
                SchedulingActivities.waitlist_fulfil, entry_id, **FOLLOW_UP
            )
        except ActivityError:
            # The booking is confirmed; the entry was cancelled or expired meanwhile. The
            # id above already stops it being promoted again, so this changes nothing.
            pass
        return appointment_id
    return None


@workflow.defn(name="CancelFollowUp")
class CancelFollowUpWorkflow:
    """What must eventually follow a cancellation that is already committed: stop the visit
    timer, refund, and offer the freed slot to the waitlist. Started best-effort after the
    cancel's own transaction (spec §4.3), so none of it can fail or delay the cancel.

    Returns "no-slot", "released" (nobody to promote) or "promoted:<appointment id>"."""

    @workflow.run
    async def run(self, request: FollowUpRequest) -> str:
        await _stop_visit(request.appointment_id)
        await workflow.execute_activity_method(
            SchedulingActivities.refund, request, **FOLLOW_UP
        )
        if request.slot_id is None:
            return "no-slot"
        promoted = await _promote_waitlist(request.slot_id)
        return f"promoted:{promoted}" if promoted else "released"


@workflow.defn(name="Reschedule")
class RescheduleWorkflow:
    """Book the new slot first, release the old one only if that confirmed (D-14): a failed
    reschedule leaves the patient with the appointment they had.

    Returns "failed" (the new booking failed and compensated itself; the old appointment
    is untouched), "rescheduled", or "superseded" -- nothing was released, because:

    - the old appointment was cancelled while the new booking ran: the new booking stands
      as the patient's only appointment;
    - the patient checked in at the old one: both stay `confirmed`. The old one closes with
      its visit; the visit settles the chain guard (`require_reschedule_settled`), so staff
      can cancel the new one if it is no longer wanted;
    - the new one was cancelled before the old one was released: the old one stands."""

    @workflow.run
    async def run(self, request: RescheduleRequest) -> str:
        try:
            await workflow.execute_child_workflow(
                BookAppointmentWorkflow.run,
                BookingRequest(
                    request.new_appointment_id, request.patient_id, request.new_slot_id,
                    request.actor_user_id, request.reason,
                    rescheduled_from_id=request.old_appointment_id,
                ),
                id=booking_workflow_id(request.new_appointment_id),
                # The new appointment's id is derived for this reschedule alone, so
                # `booking-<id>` can only be this child: a duplicate is refused rather
                # than ever answering for another booking.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                # ABANDON, as for waitlist promotions: terminating the reschedule must not
                # kill the booking mid-flight -- it compensates for itself, and the old
                # appointment is untouched until `reschedule.complete`.
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except ChildWorkflowError:
            return "failed"
        except WorkflowAlreadyStartedError:
            # Defensive: nothing else starts `booking-<new id>`. Not this reschedule's
            # booking, so the old appointment must not be released on its account.
            return "failed"
        result = await workflow.execute_activity_method(
            SchedulingActivities.reschedule_complete, request, **FOLLOW_UP
        )
        if result["outcome"] == "superseded":
            return "superseded"
        await _stop_visit(request.old_appointment_id)
        # No refund: a reschedule moves the booking rather than ending it. The released
        # slot goes to the waitlist, as a cancelled one does.
        if result["released_slot_id"] is not None:
            await _promote_waitlist(result["released_slot_id"])
        return "rescheduled"
