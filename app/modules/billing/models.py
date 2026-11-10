"""One billing record per appointment: a state machine, not a ledger (spec D-27).

    authorized --capture--> captured --refund--> refunded
         \\--void--> voided
    declined   (the pre-check refused; the booking compensates)

Billing is a pre-check and a status, not accounting (PRD section 2, non-goals): the
gateway holds the money movements, this table holds what Part A knows about them and the
gateway's references, so every operation can be repeated safely.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BillingStatus(str, enum.Enum):
    AUTHORIZED = "authorized"
    DECLINED = "declined"
    CAPTURED = "captured"
    VOIDED = "voided"
    REFUNDED = "refunded"


class BillingRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "billing_records"

    #: One per appointment, so every operation can find "the" record and act on its state.
    #: A schema-level link to scheduling's table, as the other cross-table keys are; no
    #: Python import crosses the module boundary for it.
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="RESTRICT"), unique=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[BillingStatus] = mapped_column(
        Enum(
            BillingStatus, native_enum=False, create_constraint=True, length=16,
            values_callable=lambda cls: [member.value for member in cls],
        ),
        index=True,
    )
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    #: The gateway's authorisation id; captures, voids and refunds name it.
    authorization_ref: Mapped[str | None] = mapped_column(String(100))
    decline_reason: Mapped[str | None] = mapped_column(String(200))
