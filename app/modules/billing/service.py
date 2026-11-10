"""Billing operations: authorise, capture, release. Each is idempotent twice over.

Once by the record's state -- an operation that has already happened is a no-op -- and once
by the gateway's idempotency key, which covers the window where the gateway acted but the
row was never written (the activity died, the commit failed): the retry asks again with the
same key and records the same answer.

Every change emits `billing.status_updated` through the outbox, in the same transaction.
The caller owns the transaction. A decline is *returned*, not raised: raising would roll
the row back, and the decline must be recorded; the activity raises after its commit.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.outbox import emit
from app.modules.billing.gateway import PaymentGateway
from app.modules.billing.models import BillingRecord, BillingStatus

logger = logging.getLogger(__name__)


async def _record(session: AsyncSession, appointment_id: uuid.UUID) -> BillingRecord | None:
    return await session.scalar(
        select(BillingRecord).where(BillingRecord.appointment_id == appointment_id)
        .with_for_update()
    )


def _announce(session: AsyncSession, record: BillingRecord) -> None:
    emit(session, "billing.status_updated", key=str(record.appointment_id), payload={
        "appointment_id": str(record.appointment_id),
        "patient_id": str(record.patient_id),
        "billing_status": record.status.value,
        "amount_minor": record.amount_minor,
        "currency": record.currency,
    })


async def authorize(
    session: AsyncSession, gateway: PaymentGateway, *, appointment_id: uuid.UUID,
    patient_id: uuid.UUID, amount_minor: int, currency: str,
) -> BillingRecord:
    """The booking's pre-check: place a hold. Returns the record; check its status."""
    record = await _record(session, appointment_id)
    if record is not None and record.status in (
        BillingStatus.AUTHORIZED, BillingStatus.CAPTURED, BillingStatus.DECLINED
    ):
        return record
    result = await gateway.authorize(
        key=f"authorize-{appointment_id}", amount_minor=amount_minor, currency=currency,
        customer_ref=str(patient_id), reference=str(appointment_id),
    )
    if record is None:
        record = BillingRecord(
            appointment_id=appointment_id, patient_id=patient_id,
            amount_minor=amount_minor, currency=currency,
        )
        session.add(record)
    record.status = (
        BillingStatus.DECLINED if result.status == "declined" else BillingStatus.AUTHORIZED
    )
    record.authorization_ref = result.id
    record.decline_reason = result.decline_reason
    await session.flush()
    _announce(session, record)
    return record


async def capture(
    session: AsyncSession, gateway: PaymentGateway, *, appointment_id: uuid.UUID
) -> BillingRecord | None:
    """Charge the hold (after the visit). Nothing to charge -- no record, voided,
    declined -- is not an error: the visit happened either way, and is logged."""
    record = await _record(session, appointment_id)
    if record is None or record.status is not BillingStatus.AUTHORIZED:
        if record is None or record.status is not BillingStatus.CAPTURED:
            logger.info(
                "nothing to capture",
                extra={"appointment_id": str(appointment_id),
                       "status": record.status.value if record else None},
            )
        return record
    await gateway.capture(record.authorization_ref, key=f"capture-{appointment_id}")
    record.status = BillingStatus.CAPTURED
    await session.flush()
    _announce(session, record)
    return record


async def release(
    session: AsyncSession, gateway: PaymentGateway, *, appointment_id: uuid.UUID
) -> BillingRecord | None:
    """Give the money back, whatever stage it reached: void a hold, refund a charge.
    Used by booking compensation, the cancel follow-up and a completed reschedule."""
    record = await _record(session, appointment_id)
    if record is None:
        return None
    if record.status is BillingStatus.AUTHORIZED:
        await gateway.void(record.authorization_ref, key=f"void-{appointment_id}")
        record.status = BillingStatus.VOIDED
    elif record.status is BillingStatus.CAPTURED:
        await gateway.refund(record.authorization_ref, key=f"refund-{appointment_id}")
        record.status = BillingStatus.REFUNDED
    else:
        return record
    await session.flush()
    _announce(session, record)
    return record
