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
from app.core.errors import DomainError
from app.modules.patients.public import ensure_bookable_patient
from app.modules.scheduling import service
from app.modules.scheduling.contracts import BookingRequest, VisitRequest, VisitSignal

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
        clock: Clock, no_show_grace: timedelta = timedelta(minutes=15),
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._clock = clock
        self._no_show_grace = no_show_grace

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
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
        async with self._unit_of_work() as session:
            await service.create_pending(
                session, self._audit, appointment_id=appointment_id,
                patient_id=patient_id, slot_id=slot_id,
                reason=request.reason, actor_user_id=request.actor_user_id,
            )

    @activity.defn(name="booking.validate_booking")
    async def validate_booking(self, request: BookingRequest) -> None:
        patient_id = parse_id(request.patient_id, "patient_id")
        slot_id = parse_id(request.slot_id, "slot_id")
        async with self._unit_of_work() as session:
            await ensure_bookable_patient(session, patient_id)
            await service.ensure_slot_bookable(session, slot_id=slot_id, now=self._clock.now())

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
        """Stand-in: approves. Week 3 replaces the body; a decline must raise
        `ApplicationError(non_retryable=True)` so the workflow compensates."""
        logger.info(
            "billing pre-check approved (stand-in)",
            extra={"appointment_id": request.appointment_id},
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
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await service.fail_booking(
                session, self._audit, appointment_id=appointment_id,
                actor_user_id=request.actor_user_id,
            )

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

    @activity.defn(name="visit.check_in")
    async def check_in(self, request: VisitRequest, signal: VisitSignal) -> None:
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await service.check_in(
                session, self._audit, appointment_id=appointment_id,
                now=self._clock.now(), actor_user_id=signal.actor_user_id,
            )

    @activity.defn(name="visit.seen")
    async def seen(self, request: VisitRequest, signal: VisitSignal) -> None:
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await service.mark_seen(
                session, self._audit, appointment_id=appointment_id,
                now=self._clock.now(), actor_user_id=signal.actor_user_id,
            )

    @activity.defn(name="visit.complete")
    async def complete(self, request: VisitRequest, signal: VisitSignal) -> None:
        appointment_id = parse_id(request.appointment_id, "appointment_id")
        async with self._unit_of_work() as session:
            await service.complete_visit(
                session, self._audit, appointment_id=appointment_id,
                now=self._clock.now(), actor_user_id=signal.actor_user_id,
            )

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
