"""The analytics consumer: appointment and visit events, counted into the daily rollup.

Analytics is a *consumer* of the domain events, never called by the flows it measures
(ARCHITECTURE section 3.1): booking does not know it is being counted. Each event adds to
one day's row in the handler's transaction, together with the runtime's
`processed_events` mark, so a redelivered event cannot be counted twice -- the property
that keeps Completed Visits and Average Wait Time honest (ARCHITECTURE section 3.5).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.consumer import ConsumerContext, ConsumerSpec, Event
from app.modules.analytics import service
from app.modules.analytics.models import UNKNOWN


def _id(value: str | None) -> uuid.UUID:
    return uuid.UUID(value) if value else UNKNOWN


def _at(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def on_event(session: AsyncSession, event: Event, context: ConsumerContext) -> None:
    payload = event.payload
    day = event.occurred_at.date()
    clinic, provider = _id(payload.get("clinic_id")), _id(payload.get("provider_id"))
    if event.topic == "appointments.booked":
        await service.record(session, day=day, clinic_id=clinic, provider_id=provider, booked=1)
    elif event.topic == "appointments.cancelled":
        await service.record(session, day=day, clinic_id=clinic, provider_id=provider,
                             cancelled=1)
    elif event.topic == "appointments.no_show":
        await service.record(session, day=day, clinic_id=clinic, provider_id=provider, no_show=1)
    elif event.topic == "visits.completed":
        checked_in, seen = _at(payload.get("checked_in_at")), _at(payload.get("seen_at"))
        wait = int((seen - checked_in).total_seconds()) if checked_in and seen else None
        await service.record(
            session, day=day, clinic_id=clinic, provider_id=provider, completed=1,
            **({"wait_seconds_sum": max(wait, 0), "wait_count": 1} if wait is not None else {}),
        )


ANALYTICS_CONSUMERS: tuple[ConsumerSpec, ...] = (
    ConsumerSpec(
        name="analytics.daily-rollup",
        topics=("appointments.booked", "appointments.cancelled", "appointments.no_show",
                "visits.completed"),
        handler=on_event, effect="database",
    ),
)
