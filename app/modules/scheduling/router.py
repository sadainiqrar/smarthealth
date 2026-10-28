"""Appointment routes.

Booking answers 202, not 201: the appointment is `pending` until the workflow confirms
it, and a 201 would claim a resource in a state it is not in yet (ARCHITECTURE §3.2).
A patient books and reads only their own appointments; staff act for anyone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from app.api.deps import get_session, get_temporal, page_params
from app.core.clock import Clock, get_clock
from app.core.errors import NotFound, PermissionDenied
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
    start_booking,
)
from app.modules.scheduling.contracts import BookingRequest
from app.modules.scheduling.models import AppointmentStatus
from app.modules.scheduling.schemas import (
    AppointmentRead,
    BookingAccepted,
    BookingCreate,
    SlotRead,
)

router = APIRouter(prefix="/appointments", tags=["appointments"])
slots_router = APIRouter(prefix="/slots", tags=["slots"])

BOOKERS = (UserRole.PATIENT, UserRole.FRONT_DESK, UserRole.ADMIN)
READERS = (UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK, UserRole.ADMIN)


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


@slots_router.get("", response_model=Page[SlotRead])
async def list_slots(
    provider_id: uuid.UUID,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
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
