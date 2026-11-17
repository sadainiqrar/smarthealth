"""GET /monitoring/overview and GET /monitoring/failures (system health spec §4).

Admin only. Answers 200 even when Prometheus or Jaeger is down: each card and the
`sources` block say which, so the page can show what it still knows.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.monitoring.schemas import FailuresRead, Overview
from app.modules.monitoring.service import MonitoringService

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
Hours = Literal[1, 24, 168]


def get_monitoring(request: Request) -> MonitoringService:
    return request.app.state.monitoring


@router.get("/overview", response_model=Overview)
async def overview(
    hours: Hours = 24,
    monitoring: MonitoringService = Depends(get_monitoring),
    _: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> Overview:
    """Every process's liveness, the critical flows, backlogs and AI health over the last
    `hours` (1, 24 or 168), in plain statuses with a reason each."""
    return await monitoring.overview(hours)


@router.get("/failures", response_model=FailuresRead)
async def failures(
    hours: Hours = 24,
    limit: int = Query(default=20, ge=1, le=50),
    monitoring: MonitoringService = Depends(get_monitoring),
    _: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> FailuresRead:
    """The newest error traces across the services, each reduced to its root operation,
    first failed step and message, with a link to the trace."""
    return await monitoring.failures(hours, limit)
