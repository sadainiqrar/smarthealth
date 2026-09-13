"""Patient management routes.

Front-desk staff register and update patients — the same requirement that made
`patients.user_id` nullable. Clinicians may read but not write.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_audit_log, get_session, page_params
from app.core.audit import AuditLog
from app.core.pagination import Page, PageParams
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.patients import service
from app.modules.patients.schemas import PatientCreate, PatientRead, PatientUpdate

router = APIRouter(prefix="/patients", tags=["patients"])

WRITERS = (UserRole.FRONT_DESK, UserRole.ADMIN)
READERS = (UserRole.FRONT_DESK, UserRole.ADMIN, UserRole.PROVIDER)


@router.post("", response_model=PatientRead, status_code=status.HTTP_201_CREATED)
async def register_patient(
    body: PatientCreate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*WRITERS)),
) -> PatientRead:
    patient = await service.register_patient(
        session, audit, data=body, actor_user_id=claims.subject
    )
    await session.commit()
    return PatientRead.model_validate(patient)


@router.get("/{patient_id}", response_model=PatientRead)
async def get_patient(
    patient_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*READERS)),
) -> PatientRead:
    patient = await service.get_patient(session, patient_id)
    return PatientRead.model_validate(patient)


@router.get("", response_model=Page[PatientRead])
async def list_patients(
    search: str | None = Query(default=None, max_length=100),
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*READERS)),
) -> Page[PatientRead]:
    patients, total = await service.list_patients(session, page=page, search=search)
    return Page[PatientRead](
        items=[PatientRead.model_validate(p) for p in patients],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.patch("/{patient_id}", response_model=PatientRead)
async def update_patient(
    patient_id: uuid.UUID,
    body: PatientUpdate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*WRITERS)),
) -> PatientRead:
    patient = await service.update_patient(
        session, audit, patient_id=patient_id, data=body,
        actor_user_id=claims.subject,
    )
    await session.commit()
    return PatientRead.model_validate(patient)
