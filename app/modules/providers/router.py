"""Provider management routes.

A provider record is a clinical credential, not a front-desk convenience: only an
admin creates or edits one. Reads are open to every authenticated role — a patient
needs the directory to know who they can be seen by.
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
from app.modules.providers import service
from app.modules.providers.schemas import ProviderCreate, ProviderRead, ProviderUpdate

router = APIRouter(prefix="/providers", tags=["providers"])

WRITERS = (UserRole.ADMIN,)
#: Every role, spelled `tuple(UserRole)` rather than listing four members: the rule
#: here is "any authenticated caller", so a role added later should be admitted by
#: the same rule rather than silently excluded until someone remembers this line.
#: `require_role` still enforces a valid token, so this is not "no authorisation".
READERS = tuple(UserRole)


@router.post("", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def register_provider(
    body: ProviderCreate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*WRITERS)),
) -> ProviderRead:
    provider = await service.register_provider(
        session, audit, data=body, actor_user_id=claims.subject
    )
    await session.commit()
    return ProviderRead.model_validate(provider)


@router.get("/{provider_id}", response_model=ProviderRead)
async def get_provider(
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*READERS)),
) -> ProviderRead:
    provider = await service.get_provider(session, provider_id)
    return ProviderRead.model_validate(provider)


@router.get("", response_model=Page[ProviderRead])
async def list_providers(
    specialty: str | None = Query(default=None, max_length=120),
    search: str | None = Query(default=None, max_length=100),
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*READERS)),
) -> Page[ProviderRead]:
    providers, total = await service.list_providers(
        session, page=page, specialty=specialty, search=search
    )
    return Page[ProviderRead](
        items=[ProviderRead.model_validate(p) for p in providers],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.patch("/{provider_id}", response_model=ProviderRead)
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*WRITERS)),
) -> ProviderRead:
    provider = await service.update_provider(
        session, audit, provider_id=provider_id, data=body,
        actor_user_id=claims.subject,
    )
    await session.commit()
    return ProviderRead.model_validate(provider)
