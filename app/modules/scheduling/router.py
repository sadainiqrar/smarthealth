"""Appointment routes.

Booking answers 202, not 201: the appointment is `pending` until the workflow confirms
it, and a 201 would claim a resource in a state it is not in yet (ARCHITECTURE §3.2).
Visit actions answer 200: they are written in the request's own transaction (D-12), and
so is cancel -- its follow-up (refund, waitlist) is a best-effort workflow started after the
commit. Reschedule answers 202: it must book the new slot before releasing the old one, which
only a workflow can do (D-14).
A patient books and reads only their own appointments; staff act for anyone.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.api.deps import get_audit_log, get_session, get_temporal, page_params
from app.core.audit import AuditLog
from app.core.clock import Clock, get_clock
from app.core.errors import Conflict, NotFound, PermissionDenied
from app.core.pagination import Page, PageParams
from app.core.temporal import TASK_QUEUE
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.patients.public import patient_id_for_user
from app.modules.scheduling import service
from app.modules.scheduling.booking import (
    appointment_id_for,
    booking_state,
    reschedule_appointment_id_for,
    start_booking,
)
from app.modules.scheduling.contracts import BookingRequest, FollowUpRequest, RescheduleRequest
from app.modules.scheduling.followups import start_cancel_follow_up, start_reschedule
from app.modules.scheduling.models import AppointmentStatus
from app.modules.scheduling.schemas import (
    AppointmentRead,
    BookingAccepted,
    BookingCreate,
    CancelRequest,
    RescheduleAccepted,
    RescheduleCreate,
    SlotRead,
    VisitRead,
    WaitlistCreate,
    WaitlistRead,
)
from app.modules.scheduling.state import require_cancellable
from app.modules.scheduling.visits import notify_checked_in

router = APIRouter(prefix="/appointments", tags=["appointments"])
slots_router = APIRouter(prefix="/slots", tags=["slots"])
waitlist_router = APIRouter(prefix="/waitlist", tags=["waitlist"])

BOOKERS = (UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
#: Cancelling and rescheduling take the same people as booking: a patient acts only on
#: their own appointments, staff on anyone's.
CANCELLERS = BOOKERS
CHECKERS_IN = (UserRole.FRONT_DESK, UserRole.ADMIN)
CLINICIANS = (UserRole.PROVIDER, UserRole.ADMIN)
READERS = (UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK, UserRole.ADMIN)
#: Who may list the waitlist: a patient (their own entries only) and the staff who manage
#: it (spec 4.5). Not providers -- the list names every patient waiting, for any provider.
WAITLIST_READERS = BOOKERS


async def _own_patient_id(session: AsyncSession, claims: TokenClaims) -> uuid.UUID | None:
    return await patient_id_for_user(session, uuid.UUID(claims.subject))


@router.post("", response_model=BookingAccepted, status_code=status.HTTP_202_ACCEPTED)
async def book_appointment(
    body: BookingCreate,
    request: Request,
    # min_length=1: an empty key is still a key, and every keyless-looking request from
    # one caller would collapse onto a single appointment id.
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
    session: AsyncSession = Depends(get_session),
    temporal: Client = Depends(get_temporal),
    claims: TokenClaims = Depends(require_role(*BOOKERS)),
) -> BookingAccepted:
    if claims.role is UserRole.PATIENT:
        if await _own_patient_id(session, claims) != body.patient_id:
            raise PermissionDenied("a patient may only book for themselves")

    appointment_id = appointment_id_for(claims.subject, idempotency_key)
    await start_booking(
        temporal,
        BookingRequest(
            appointment_id=str(appointment_id),
            patient_id=str(body.patient_id),
            slot_id=str(body.slot_id),
            actor_user_id=claims.subject,
            reason=body.reason,
        ),
        task_queue=request.app.state.settings.task_queue(TASK_QUEUE),
    )
    return BookingAccepted(appointment_id=appointment_id)


@router.get("/{appointment_id}", response_model=AppointmentRead)
async def get_appointment(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    temporal: Client = Depends(get_temporal),
    claims: TokenClaims = Depends(require_role(*READERS)),
) -> AppointmentRead:
    appointment = await service.get_appointment(session, appointment_id)
    if appointment is None:
        # No row yet: ask Temporal. These synthetic answers carry only id, status and
        # in_progress -- there is no row to read anything else from.
        state = await booking_state(temporal, appointment_id)
        if state == "running":
            return AppointmentRead(
                id=appointment_id, status=AppointmentStatus.PENDING, in_progress=True
            )
        if state == "closed":
            # The workflow ended before it recorded the appointment: the booking failed.
            return AppointmentRead(id=appointment_id, status=AppointmentStatus.FAILED)
        raise NotFound(f"appointment {appointment_id} does not exist")
    # 404 rather than 403 for someone else's appointment: a patient must not be able to
    # probe which appointment ids exist.
    if claims.role is UserRole.PATIENT:
        if await _own_patient_id(session, claims) != appointment.patient_id:
            raise NotFound(f"appointment {appointment_id} does not exist")
    return AppointmentRead.model_validate(appointment)


# The visit actions write synchronously and the router owns the commit (the Week 1 rule).
# Each service call locks the appointment row and checks the transition under that lock,
# so the write *is* the check: an illegal action is a 409 and an unknown appointment a
# 404, with no separate precheck to go stale.


@router.post("/{appointment_id}/check-in", response_model=VisitRead)
async def check_in(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    temporal: Client = Depends(get_temporal),
    clock: Clock = Depends(get_clock),
    claims: TokenClaims = Depends(require_role(*CHECKERS_IN)),
) -> VisitRead:
    visit = await service.check_in(
        session, audit, appointment_id=appointment_id, now=clock.now(),
        actor_user_id=claims.subject,
    )
    await session.commit()
    # After the commit, and never failing the request: the check-in is recorded, and the
    # no-show timer reconciles against the database if this signal is lost.
    await notify_checked_in(temporal, appointment_id)
    return VisitRead.model_validate(visit)


@router.post("/{appointment_id}/seen", response_model=VisitRead)
async def mark_seen(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    clock: Clock = Depends(get_clock),
    claims: TokenClaims = Depends(require_role(*CLINICIANS)),
) -> VisitRead:
    visit = await service.mark_seen(
        session, audit, appointment_id=appointment_id, now=clock.now(),
        actor_user_id=claims.subject,
    )
    await session.commit()
    return VisitRead.model_validate(visit)


@router.post("/{appointment_id}/complete", response_model=VisitRead)
async def complete_visit(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    clock: Clock = Depends(get_clock),
    claims: TokenClaims = Depends(require_role(*CLINICIANS)),
) -> VisitRead:
    visit = await service.complete_visit(
        session, audit, appointment_id=appointment_id, now=clock.now(),
        actor_user_id=claims.subject,
    )
    await session.commit()
    return VisitRead.model_validate(visit)


# Cancel is a synchronous paired write -- appointment cancelled and slot freed in one
# transaction, under the appointment lock -- so the caller learns the outcome from the
# response. The follow-up (stop the visit timer, refund, offer the slot to the waitlist)
# starts after the commit and can never fail the request.


async def _owned_or_404(session: AsyncSession, claims: TokenClaims, appointment_id: uuid.UUID):
    """The appointment, freshly read. 404 for a missing one and, for a patient, for someone
    else's -- so ids cannot be probed, as in GET appointment."""
    appointment = await service.get_appointment(session, appointment_id)
    if appointment is None:
        raise NotFound(f"appointment {appointment_id} does not exist")
    if claims.role is UserRole.PATIENT:
        if await _own_patient_id(session, claims) != appointment.patient_id:
            raise NotFound(f"appointment {appointment_id} does not exist")
    return appointment


@router.post("/{appointment_id}/cancel", response_model=AppointmentRead)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    body: CancelRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    temporal: Client = Depends(get_temporal),
    clock: Clock = Depends(get_clock),
    claims: TokenClaims = Depends(require_role(*CANCELLERS)),
) -> AppointmentRead:
    await _owned_or_404(session, claims, appointment_id)
    appointment = await service.cancel_appointment(
        session, audit, appointment_id=appointment_id, reason=body.reason,
        now=clock.now(), actor_user_id=claims.subject,
    )
    read = AppointmentRead.model_validate(appointment)
    follow_up = FollowUpRequest(
        appointment_id=str(appointment_id),
        slot_id=str(appointment.slot_id) if appointment.slot_id else None,
        actor_user_id=claims.subject,
    )
    await session.commit()
    # After the commit, and never failing the request: the cancellation is recorded, and a
    # lost start only delays the refund and the waitlist (a known gap, Week 3 outbox).
    await start_cancel_follow_up(
        temporal, follow_up, task_queue=request.app.state.settings.task_queue(TASK_QUEUE)
    )
    return read


@router.post(
    "/{appointment_id}/reschedule", response_model=RescheduleAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reschedule_appointment(
    appointment_id: uuid.UUID,
    body: RescheduleCreate,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=200
    ),
    session: AsyncSession = Depends(get_session),
    temporal: Client = Depends(get_temporal),
    claims: TokenClaims = Depends(require_role(*CANCELLERS)),
) -> RescheduleAccepted:
    old = await _owned_or_404(session, claims, appointment_id)
    new_appointment_id = reschedule_appointment_id_for(claims.subject, idempotency_key)
    # A precheck, so the common refusals are answered now rather than as a workflow that
    # fails later. The workflow still decides under the appointment lock (`superseded`);
    # this only spares the caller a 202 for a request that cannot succeed.
    #
    # A repeat of an accepted request must not trip it: once the first reschedule completes
    # the old appointment is `rescheduled`, and the repeat would be a 409 -- yet an
    # Idempotency-Key promises the answer the first request got. The new appointment row,
    # linked back to this one, is how a repeat is recognised.
    existing = await service.get_appointment(session, new_appointment_id)
    repeat = existing is not None and existing.rescheduled_from_id == appointment_id
    if not repeat:
        require_cancellable(
            old.status, await service.get_visit(session, appointment_id) is not None
        )
        if old.status is not AppointmentStatus.CONFIRMED:
            raise Conflict(
                f"a {old.status.value} appointment cannot be rescheduled; only a confirmed "
                "one can"
            )
        if old.rescheduled_from_id is not None:
            predecessor = await service.get_appointment(session, old.rescheduled_from_id)
            await service.require_reschedule_settled(session, predecessor)
    await start_reschedule(
        temporal,
        RescheduleRequest(
            old_appointment_id=str(appointment_id),
            new_appointment_id=str(new_appointment_id),
            patient_id=str(old.patient_id),
            new_slot_id=str(body.new_slot_id),
            actor_user_id=claims.subject,
            reason=body.reason,
        ),
        task_queue=request.app.state.settings.task_queue(TASK_QUEUE),
    )
    return RescheduleAccepted(
        appointment_id=new_appointment_id, rescheduled_from=appointment_id
    )


@router.get("/{appointment_id}/visit", response_model=VisitRead)
async def get_visit(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    claims: TokenClaims = Depends(require_role(*READERS)),
) -> VisitRead:
    appointment = await service.get_appointment(session, appointment_id)
    if appointment is None:
        raise NotFound(f"appointment {appointment_id} does not exist")
    # Same rule as GET appointment: 404 for someone else's, so ids cannot be probed.
    if claims.role is UserRole.PATIENT:
        if await _own_patient_id(session, claims) != appointment.patient_id:
            raise NotFound(f"appointment {appointment_id} does not exist")
    visit = await service.get_visit(session, appointment_id)
    if visit is None:
        raise NotFound(f"appointment {appointment_id} has no visit yet")
    return VisitRead.model_validate(visit)


@slots_router.get("", response_model=Page[SlotRead])
async def list_slots(
    provider_id: uuid.UUID,
    # AwareDatetime: a timestamp without an offset is ambiguous, and comparing it with
    # the aware `now` would raise -- a 500. Rejected here as a 422 instead.
    from_: AwareDatetime | None = Query(default=None, alias="from"),
    to: AwareDatetime | None = Query(default=None),
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    _: TokenClaims = Depends(require_role(*UserRole)),
) -> Page[SlotRead]:
    slots, total = await service.list_free_slots(
        session, provider_id=provider_id, now=clock.now(), start=from_, end=to, page=page
    )
    return Page[SlotRead](
        items=[SlotRead.model_validate(s) for s in slots],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@waitlist_router.post("", response_model=WaitlistRead, status_code=status.HTTP_201_CREATED)
async def join_waitlist(
    body: WaitlistCreate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*BOOKERS)),
) -> WaitlistRead:
    if claims.role is UserRole.PATIENT:
        if await _own_patient_id(session, claims) != body.patient_id:
            raise PermissionDenied("a patient may only join the waitlist for themselves")
    entry = await service.add_waitlist_entry(
        session, audit, patient_id=body.patient_id, provider_id=body.provider_id,
        department_id=body.department_id, window_start=body.window_start,
        window_end=body.window_end, actor_user_id=claims.subject,
    )
    read = WaitlistRead.model_validate(entry)
    await session.commit()
    return read


@waitlist_router.get("", response_model=Page[WaitlistRead])
async def list_waitlist(
    patient_id: uuid.UUID | None = None,
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    claims: TokenClaims = Depends(require_role(*WAITLIST_READERS)),
) -> Page[WaitlistRead]:
    if claims.role is UserRole.PATIENT:
        # Forced, not filtered: whatever `patient_id` a patient sends, they see their own.
        patient_id = await _own_patient_id(session, claims)
        if patient_id is None:
            # No patient record means no entries -- and `None` would mean "everyone".
            return Page[WaitlistRead](items=[], total=0, limit=page.limit, offset=page.offset)
    entries, total = await service.list_waitlist(session, patient_id=patient_id, page=page)
    return Page[WaitlistRead](
        items=[WaitlistRead.model_validate(e) for e in entries],
        total=total, limit=page.limit, offset=page.offset,
    )
