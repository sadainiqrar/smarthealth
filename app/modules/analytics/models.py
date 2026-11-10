"""The daily rollup the five Part A metrics are read from (PART-A-FR-5).

One row per (day, clinic, provider), incremented by the analytics consumer as events
arrive, in the same transaction as its `processed_events` mark -- so a redelivered event
cannot count twice. These are business numbers: they live in Postgres, not Prometheus,
whose counters reset when a process restarts and which is no system of record.

Which day an event counts on is the day it *happened*: a booking on the day it was
confirmed, a cancellation on the day it was cancelled, a visit on the day it completed.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Date, Integer
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: The key for events that do not say which clinic (none of ours, since schema v2, but a
#: v1 event replayed from a topic's start might not).
UNKNOWN = uuid.UUID(int=0)


class AnalyticsDaily(Base):
    __tablename__ = "analytics_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    clinic_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    booked: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    no_show: Mapped[int] = mapped_column(Integer, default=0)
    #: Average Wait Time = wait_seconds_sum / wait_count, over the visits that were seen
    #: (PRD D-3: seen_at - checked_in_at).
    wait_seconds_sum: Mapped[int] = mapped_column(BigInteger, default=0)
    wait_count: Mapped[int] = mapped_column(Integer, default=0)
