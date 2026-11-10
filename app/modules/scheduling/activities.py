"""Booking activities: one service call, one transaction, per activity.

Each activity opens its own session and commits, so a retried activity re-runs exactly
one unit of work against the idempotent service functions. Domain errors become
non-retryable `ApplicationError`s -- a lost slot race or a past slot will not change on
retry, and retrying it only delays compensation. Anything else (a dropped connection)
propagates and Temporal retries it.

Every activity carries an explicit `booking.` name. Temporal routes activities by name
alone, and the default is the bare method name -- slice 3's visit activities (another
`confirm`, say) would otherwise collide with these on the shared task queue.

Billing and reminders are stand-ins until Week 3 (spec §1): the workflow has its final
shape now, and Week 3 replaces these bodies, not the workflow.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.core.audit import AuditLog
from app.core.clock import Clock
from app.core.errors import Conflict, DomainError, NotFound, ServiceUnavailable
import app.modules.billing.public as billing
from app.modules.patients.public import ensure_bookable_patient
from app.modules.providers.public import ensure_bookable_provider
from app.modules.scheduling import service
from app.modules.scheduling.contracts import (
    BookingRequest,
    FollowUpRequest,
    PostVisitRequest,
    ProviderDeactivationRequest,
    RescheduleRequest,
    VisitRequest,
)

logger = logging.getLogger(__name__)


def parse_id(value: str, field: str) -> uuid.UUID:
    """A request id as a UUID, or a non-retryable failure.

    Called before the unit of work opens. A malformed id raises `ValueError`, which is
    not a `DomainError`, so left alone it would propagate as a retryable failure and be
    retried on every step until the attempts run out -- and forever under compensation's
    unlimited policy. It can never parse on a retry, so it fails once, permanently.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApplicationError(
            f"{field} is not a valid id: {value!r}", type="InvalidRequest", non_retryable=True
        ) from exc


class SchedulingActivities:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], audit: AuditLog,
        clock: Clock, no_show_grace: timedelta, *, gateway: billing.PaymentGateway,
        amount_minor: int, currency: str,
    ) -> None:
        # `no_show_grace` has no default: the one default is `Settings`, and a second
        # here could silently disagree with it at any construction site that forgot it.
        self._session_factory = session_factory
        self._audit = audit
        self._clock = clock
        self._no_show_grace = no_show_grace
        self._gateway = gateway
        self._amount_minor = amount_minor
        self._currency = currency

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except ServiceUnavailable as exc:
                # A dependency is down (the payment gateway, its circuit open): retryable,
                # unlike every other DomainError. Temporal retries with backoff; failing the
                # booking permanently over an outage would be the wrong answer.
                await session.rollback()
                raise ApplicationError(
                    exc.detail, type=type(exc).__name__, non_retryable=False
                ) from exc
            except DomainError as exc:
                await session.rollback()
                raise ApplicationError(
                    exc.detail, type=type(exc).__name__, non_retryable=True
                ) from exc
            except Exception:
                # `Exception`, not `BaseException`: a cancellation (CancelledError) must
                # propagate untouched, and awaiting a rollback in the middle of one can
                # itself be cancelled. The session's `async with` exit rolls back an open
                # transaction on any exit, so nothing is left dirty either way.
                try:
                    await session.rollback()
                except Exception:
                    # A rollback on a dead connection fails too; that second error must
                    # not replace the original, which is the one worth retrying on.
                    logger.warning("rollback failed after an activity error", exc_info=True)
                raise

    @activity.defn(name="booking.create_pending")
    async def create_pending(self, request: BookingRequest) -> None:
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        patient_id = parse_id(request.patient_id, "patient_id")
        slot_id = parse_id(request.slot_id, "slot_id")
        rescheduled_from_id = (
            parse_id(request.rescheduled_from_id, "rescheduled_from_id")
            if request.rescheduled_from_id is not None else None
        )
        async with self._unit_of_work() as session:
            await service.create_pending(
                session, self._audit, appointment_id=appointment_id,
                patient_id=patient_id, slot_id=slot_id,
                reason=request.reason, actor_user_id=request.actor_user_id,
                rescheduled_from_id=rescheduled_from_id,
                origin=request.origin, conversation_ref=request.conversation_ref,
            )

    @activity.defn(name="booking.validate_booking")
    async def validate_booking(self, request: BookingRequest) -> None:
        patient_id = parse_id(request.patient_id, "patient_id")
        slot_id = parse_id(request.slot_id, "slot_id")
        async with self._unit_of_work() as session:
            await ensure_bookable_patient(session, patient_id)
            await service.ensure_slot_bookable(session, slot_id=slot_id, now=self._clock.now())
            # After the slot check, which has already said NotFound for a missing slot.
            # A deactivated provider takes no new bookings (PRD R-24). Checked here, not
            # in `claim_slot`: the service may not import `providers` (D-15), and a
            # deactivation between this step and the claim is the same window every
            # validation has -- what happens to bookings that already exist is R-25.
            provider_id = await service.slot_provider_id(session, slot_id)
            if provider_id is not None:
                await ensure_bookable_provider(session, provider_id)

    @activity.defn(name="booking.claim_slot")
    async def claim_slot(self, request: BookingRequest) -> None:
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        slot_id = parse_id(request.slot_id, "slot_id")
        async with self._unit_of_work() as session:
            await service.claim_slot(
                session, self._audit, appointment_id=appointment_id,
                slot_id=slot_id, now=self._clock.now(),
                actor_user_id=request.actor_user_id,
            )

    @activity.defn(name="booking.billing_precheck")
    async def billing_precheck(self, request: BookingRequest) -> None:
        """Authorise the visit's price with the payment gateway (a hold, captured after the
        visit). A decline is recorded, then raised as non-retryable, so the booking
        compensates -- and compensation releases nothing, because nothing was held."""
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        patient_id = parse_id(request.patient_id, "patient_id")
        async with self._unit_of_work() as session:
            record = await billing.authorize(
                session, self._gateway, appointment_id=appointment_id, patient_id=patient_id,
                amount_minor=self._amount_minor, currency=self._currency,
            )
        if record.status is billing.BillingStatus.DECLINED:
            raise ApplicationError(
                f"payment declined: {record.decline_reason or 'no reason given'}",
                type="PaymentDeclined", non_retryable=True,
            )

    @activity.defn(name="booking.schedule_reminders")
    async def schedule_reminders(self, request: BookingRequest) -> None:
        """Stand-in: logs. Week 3 enqueues delivery through Celery."""
        logger.info(
            "reminders scheduled (stand-in)",
            extra={"appointment_id": request.appointment_id},
        )

    @activity.defn(name="booking.confirm")
    async def confirm(self, request: BookingRequest) -> None:
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await service.confirm_booking(
                session, self._audit, appointment_id=appointment_id,
                now=self._clock.now(), actor_user_id=request.actor_user_id,
            )

    @activity.defn(name="booking.fail_booking")
    async def fail_booking(self, request: BookingRequest) -> None:
        """Compensation: the appointment and its slot first, then the money. Separate
        transactions on purpose -- a gateway outage must not keep the slot held; the slot
        is released and committed, and only the billing half retries."""
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await service.fail_booking(
                session, self._audit, appointment_id=appointment_id,
                actor_user_id=request.actor_user_id,
            )
        async with self._unit_of_work() as session:
            await billing.release(session, self._gateway, appointment_id=appointment_id)

    @activity.defn(name="visit.deadline")
    async def deadline(self, request: VisitRequest) -> str:
        """When the appointment becomes a no-show: slot end plus grace, as an ISO string
        so the default converter round-trips it."""
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            when = await service.no_show_deadline(
                session, appointment_id=appointment_id, grace=self._no_show_grace
            )
        return when.isoformat()

    @activity.defn(name="visit.no_show")
    async def no_show(self, request: VisitRequest) -> str:
        """A `NoShowOutcome` value: "no_show", "visit_exists" or "closed". Only the
        first writes anything."""
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            outcome = await service.mark_no_show(
                session, self._audit, appointment_id=appointment_id,
                actor_user_id="system:visit-workflow",
            )
        return outcome.value

    @activity.defn(name="cancel.refund")
    async def refund(self, request: FollowUpRequest) -> None:
        """Give the money back for a cancelled appointment: void the hold, or refund the
        charge if it was captured. Idempotent by the billing record's state and by the
        gateway key, so a retried follow-up never refunds twice."""
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await billing.release(session, self._gateway, appointment_id=appointment_id)

    @activity.defn(name="reschedule.complete")
    async def reschedule_complete(self, request: RescheduleRequest) -> dict[str, str | None]:
        """`{"outcome": "rescheduled" | "superseded", "released_slot_id": str | None}` --
        JSON-friendly, so the default converter round-trips it. The slot id is what the
        workflow offers the waitlist. The new appointment is passed too: the old one is
        released only while the new one is still the confirmed replacement."""
        old_appointment_id = parse_id(request.old_appointment_id, "old_appointment_id")
        new_appointment_id = parse_id(request.new_appointment_id, "new_appointment_id")
        async with self._unit_of_work() as session:
            result = await service.complete_reschedule(
                session, self._audit, old_appointment_id=old_appointment_id,
                new_appointment_id=new_appointment_id, actor_user_id=request.actor_user_id,
            )
        if result.outcome == "rescheduled":
            # The replacement holds its own authorisation (its booking's pre-check); the old
            # appointment's is released, after the reschedule committed.
            async with self._unit_of_work() as session:
                await billing.release(session, self._gateway, appointment_id=old_appointment_id)
        return {
            "outcome": result.outcome,
            "released_slot_id": (
                str(result.released_slot_id) if result.released_slot_id else None
            ),
        }

    @activity.defn(name="post_visit.charge")
    async def post_visit_charge(self, request: PostVisitRequest) -> str | None:
        """Capture the booking's authorisation now the visit happened. Returns the billing
        status, or None when there was nothing to charge."""
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            record = await billing.capture(session, self._gateway, appointment_id=appointment_id)
        return record.status.value if record else None

    @activity.defn(name="provider.future_appointments")
    async def provider_future_appointments(self, request: ProviderDeactivationRequest) -> list[str]:
        provider_id = parse_id(request.provider_id, "provider_id")
        async with self._unit_of_work() as session:
            ids = await service.future_live_appointments(
                session, provider_id=provider_id, now=self._clock.now()
            )
        return [str(appointment_id) for appointment_id in ids]

    @activity.defn(name="provider.cancel_appointment")
    async def provider_cancel_appointment(self, appointment_id: str) -> str:
        """Cancel one of a deactivated provider's appointments (spec D-23). "cancelled", or
        "skipped" when it is no longer cancellable -- the patient checked in, someone else
        cancelled it -- which is the right end for it, not a failure. The cancel writes
        `appointments.cancelled`, whose consumer refunds and offers the slot on."""
        parsed = parse_id(appointment_id, "appointment_id")
        try:
            async with self._session_factory() as session:
                await service.cancel_appointment(
                    session, self._audit, appointment_id=parsed,
                    reason="provider_unavailable", now=self._clock.now(),
                    actor_user_id="system:provider-deactivation",
                )
                await session.commit()
        except (Conflict, NotFound):
            return "skipped"
        return "cancelled"

    @activity.defn(name="waitlist.candidates")
    async def waitlist_candidates(self, slot_id: str) -> list[list[str]]:
        """`[[entry_id, patient_id], ...]`, oldest first, as strings."""
        parsed = parse_id(slot_id, "slot_id")
        async with self._unit_of_work() as session:
            candidates = await service.waitlist_candidates(
                session, slot_id=parsed, now=self._clock.now()
            )
        return [[str(entry_id), str(patient_id)] for entry_id, patient_id in candidates]

    @activity.defn(name="waitlist.fulfil")
    async def waitlist_fulfil(self, entry_id: str) -> None:
        parsed = parse_id(entry_id, "entry_id")
        async with self._unit_of_work() as session:
            await service.fulfil_waitlist_entry(
                session, self._audit, entry_id=parsed,
                actor_user_id="system:waitlist",
            )
