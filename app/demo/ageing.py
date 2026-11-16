"""Ageing for imported clinical state.

`import_day` judges an order reviewed and a prescription expired against the run's `now`, and a
day is never imported twice, so without this pass an order that was recent at import stays
`ordered` and a prescription stays `active` for ever. `age_history` applies the same two rules
again with a later `now`. It is idempotent and touches only rows hanging off `legacy_import`
appointments of the demo providers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.history import ORIGIN, REVIEW_AFTER, _result_document
from app.demo.network import Network
from app.modules.clinical.models import (
    ClinicalNote,
    OrderStatus,
    Prescription,
    PrescriptionStatus,
    TestOrder,
)
from app.modules.clinical.storage import DocumentStore
from app.modules.scheduling.models import Appointment, Visit


@dataclass
class AgeingCounts:
    orders_reviewed: int = 0
    prescriptions_completed: int = 0


def _days(duration: str) -> int:
    found = re.match(r"\s*(\d+)", duration)
    return int(found.group(1)) if found else 0


async def age_history(session: AsyncSession, net: Network, *, store: DocumentStore,
                      now: datetime) -> AgeingCounts:
    """Review orders still `ordered` whose visit completed over REVIEW_AFTER before `now`
    (result document attached and linked both ways, as `import_day` does) and complete the
    prescriptions whose duration has elapsed. Flushes, never commits."""
    counts = AgeingCounts()
    legacy = (Appointment.origin == ORIGIN,
              Appointment.provider_id.in_(list(net.provider_ids.values())))
    orders = (await session.execute(
        select(TestOrder, Visit.completed_at)
        .join(ClinicalNote, ClinicalNote.id == TestOrder.note_id)
        .join(Appointment, Appointment.id == ClinicalNote.appointment_id)
        .join(Visit, Visit.id == ClinicalNote.visit_id)
        .where(*legacy, TestOrder.status == OrderStatus.ORDERED,
               Visit.completed_at < now - REVIEW_AFTER))).all()
    for order, completed_at in orders:
        order.status = OrderStatus.REVIEWED
        order.reviewed_at = completed_at + timedelta(days=7)
        order.result_document_id = await _result_document(
            session, store, order.patient_id, order.test_code, completed_at, order.id)
        counts.orders_reviewed += 1
    prescriptions = (await session.execute(
        select(Prescription, Visit.completed_at)
        .join(ClinicalNote, ClinicalNote.id == Prescription.note_id)
        .join(Appointment, Appointment.id == ClinicalNote.appointment_id)
        .join(Visit, Visit.id == ClinicalNote.visit_id)
        .where(*legacy, Prescription.status == PrescriptionStatus.ACTIVE))).all()
    for prescription, completed_at in prescriptions:
        if completed_at + timedelta(days=_days(prescription.duration)) <= now:
            prescription.status = PrescriptionStatus.COMPLETED
            counts.prescriptions_completed += 1
    await session.flush()
    return counts
