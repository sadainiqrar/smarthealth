"""The clinical record (Weeks 3-5 spec section 9.6, D-31).

What a visit produced -- the doctor's note with its diagnoses, the prescriptions and the
tests ordered -- and the reports patients bring back. Transactional patient data, so Part
A owns it, with the same audit and consistency as appointments; none of its content ever
travels on Kafka (events carry ids and statuses only).

A note is versioned: a signed version is never edited. Amending it starts a new draft
version (`version + 1`), and the earlier version stays readable. Exactly one version of a
visit's note can be a draft at a time.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _enum(enum_class: type[enum.Enum], length: int) -> Enum:
    return Enum(enum_class, native_enum=False, create_constraint=True, length=length,
                values_callable=lambda cls: [member.value for member in cls])


class NoteStatus(str, enum.Enum):
    DRAFT = "draft"
    SIGNED = "signed"


class OrderStatus(str, enum.Enum):
    ORDERED = "ordered"
    RESULT_UPLOADED = "result_uploaded"
    REVIEWED = "reviewed"


class PrescriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    STOPPED = "stopped"


class ClinicalNote(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "clinical_notes"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("visits.id", ondelete="RESTRICT"))
    appointment_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[NoteStatus] = mapped_column(_enum(NoteStatus, 8))
    presenting_complaint: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[str] = mapped_column(Text, default="")
    #: For clinicians only; a patient never sees it.
    clinician_notes: Mapped[str] = mapped_column(Text, default="")
    #: Plain language, for the patient.
    patient_summary: Mapped[str] = mapped_column(Text, default="")
    #: [{"description": ..., "icd10": ... | null}] -- free text required, a code optional
    #: (spec O-6). Inside the version, because a diagnosis is part of what was signed.
    diagnoses: Mapped[list] = mapped_column(JSONB, default=list)
    #: The prescriptions and orders as drafted; they become rows when the note is signed.
    draft_prescriptions: Mapped[list] = mapped_column(JSONB, default=list)
    draft_orders: Mapped[list] = mapped_column(JSONB, default=list)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ux_clinical_notes_visit_version", "visit_id", "version", unique=True),
        # At most one draft per visit: two clinicians cannot each be editing a version.
        Index("ux_clinical_notes_one_draft", "visit_id", unique=True,
              postgresql_where=text("status = 'draft'")),
    )


class Prescription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "prescriptions"

    note_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinical_notes.id", ondelete="RESTRICT"))
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    medication: Mapped[str] = mapped_column(String(200))
    dose: Mapped[str] = mapped_column(String(100))
    frequency: Mapped[str] = mapped_column(String(100))
    duration: Mapped[str] = mapped_column(String(100), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[PrescriptionStatus] = mapped_column(_enum(PrescriptionStatus, 16))


class TestOrder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "test_orders"
    __test__ = False  # not a pytest class, whatever its name

    note_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinical_notes.id", ondelete="RESTRICT"))
    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    ordered_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    #: From the test catalogue (app/modules/clinical/catalogue.py), which also names the
    #: prep guide in the knowledge corpus.
    test_code: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(Text, default="")
    urgency: Mapped[str] = mapped_column(String(16), default="routine")
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus, 16), index=True)
    result_document_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PatientDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An uploaded report. The bytes live in object storage (MinIO); this row is the record
    of them -- who uploaded what, its hash, and the text extracted from it."""

    __tablename__ = "patient_documents"

    patient_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(300), unique=True)
    test_order_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    #: pending -> extracted | not_extractable (images and scanned PDFs: no OCR) | failed
    extraction_status: Mapped[str] = mapped_column(String(20), default="pending")
    extracted_text: Mapped[str | None] = mapped_column(Text)
