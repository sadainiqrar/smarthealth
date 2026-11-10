"""GET /analytics/summary and GET /analytics/timeseries (PART-A-FR-5).

Read from the daily rollup the analytics consumer keeps, never from the operational
tables. Admin and front desk only: these are clinic-wide numbers.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.patients.public as patients
from app.api.deps import get_session
from app.core.clock import Clock, get_clock
from app.core.errors import Unprocessable
from app.modules.analytics import service
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims

router = APIRouter(prefix="/analytics", tags=["analytics"])
VIEWERS = (UserRole.ADMIN, UserRole.FRONT_DESK)
#: The longest window one request may aggregate; a year of days is plenty for a dashboard.
MAX_DAYS = 366


class SummaryRead(BaseModel):
    start: date
    end: date
    total_patients: int
    appointments_booked: int
    completed_visits: int
    cancellations: int
    no_shows: int
    cancellation_rate: float | None
    average_wait_seconds: float | None


class Point(BaseModel):
    day: date
    value: float | None


class TimeseriesRead(BaseModel):
    metric: str
    points: list[Point]


def _window(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    end = end or today
    start = start or end - timedelta(days=29)
    if start > end:
        raise Unprocessable("`from` is after `to`")
    if (end - start).days >= MAX_DAYS:
        raise Unprocessable(f"a window is at most {MAX_DAYS} days")
    return start, end


@router.get("/summary", response_model=SummaryRead)
async def get_summary(
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    clinic_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    _: TokenClaims = Depends(require_role(*VIEWERS)),
) -> SummaryRead:
    """The five metrics over a window (default: the last 30 days). Total Patients is the
    register's size now, not a count over the window."""
    start, end = _window(start, end, clock.now().date())
    totals = await service.summary(session, start=start, end=end, clinic_id=clinic_id)
    return SummaryRead(
        start=start, end=end, total_patients=await patients.count_patients(session),
        appointments_booked=totals.booked, completed_visits=totals.completed,
        cancellations=totals.cancelled, no_shows=totals.no_show,
        cancellation_rate=totals.cancellation_rate,
        average_wait_seconds=totals.average_wait_seconds,
    )


@router.get("/timeseries", response_model=TimeseriesRead)
async def get_timeseries(
    metric: Literal["booked", "cancelled", "completed", "no_show", "average_wait_seconds"],
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    clinic_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    _: TokenClaims = Depends(require_role(*VIEWERS)),
) -> TimeseriesRead:
    """One point per day with data: Appointments Booked Over Time is `metric=booked`."""
    start, end = _window(start, end, clock.now().date())
    points = await service.timeseries(
        session, metric=metric, start=start, end=end, clinic_id=clinic_id
    )
    return TimeseriesRead(metric=metric, points=[Point(day=d, value=v) for d, v in points])
