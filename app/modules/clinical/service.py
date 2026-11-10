"""The clinical record's operations (spec section 9.6).

Framework-free and boundary-clean like every service: it is handed ids and facts (which
visit, whose, by whom) by the router, which resolved them through the other modules'
published interfaces, and it decides nothing about *who may* -- the router does, before it
calls. It owns the rules about the record itself: a signed note is never edited, an
amendment is a new version, one draft at a time, signing turns drafted prescriptions and
orders into rows.

Every write is audited with before and after, and so is every read of a patient's record
(`record_viewed`): with clinical data, who looked matters as much as who changed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound, PermissionDenied, Unprocessable
from app.events.outbox import emit
from app.modules.clinical.catalogue import CATALOGUE
from app.modules.clinical.models import (
    ClinicalNote,
    NoteStatus,
    OrderStatus,
    PatientDocument,
    Prescription,
    PrescriptionStatus,
    TestOrder,
)


@dataclass(frozen=True)
class VisitFacts:
    """What the router learned about the visit from scheduling's published interface."""

    visit_id: uuid.UUID
    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    provider_id: uuid.UUID


async def _audit(audit: AuditLog, entity: str, entity_id: uuid.UUID, action: str,
                 actor: str | None, before: dict | None = None, after: dict | None = None):
    await audit.record(AuditEvent(entity_type=entity, entity_id=str(entity_id), action=action,
                                  actor_user_id=actor, before=before, after=after))


def _note_snapshot(note: ClinicalNote) -> dict[str, Any]:
    return {"version": note.version, "status": note.status.value,
            "diagnoses": note.diagnoses, "prescriptions": len(note.draft_prescriptions),
            "orders": len(note.draft_orders)}


async def latest_note(
    session: AsyncSession, appointment_id: uuid.UUID, *, for_update: bool = False
) -> ClinicalNote | None:
    statement = (select(ClinicalNote).where(ClinicalNote.appointment_id == appointment_id)
                 .order_by(ClinicalNote.version.desc()).limit(1))
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def latest_signed_note(
    session: AsyncSession, appointment_id: uuid.UUID
) -> ClinicalNote | None:
    return await session.scalar(
        select(ClinicalNote).where(ClinicalNote.appointment_id == appointment_id,
                                   ClinicalNote.status == NoteStatus.SIGNED)
        .order_by(ClinicalNote.version.desc()).limit(1))


def _validate(body: dict[str, Any]) -> None:
    for order in body.get("orders", []):
        if order["test_code"] not in CATALOGUE:
            raise Unprocessable(f"unknown test {order['test_code']!r}; see the test catalogue")
    for diagnosis in body.get("diagnoses", []):
        if not diagnosis.get("description", "").strip():
            raise Unprocessable("a diagnosis needs a description (an ICD-10 code is optional)")


async def save_draft(
    session: AsyncSession, audit: AuditLog, *, visit: VisitFacts, author_provider_id: uuid.UUID,
    body: dict[str, Any], actor: str,
) -> ClinicalNote:
    """Create the visit's first draft, or update the current draft. A signed latest version
    is a Conflict: amend it (a new version) rather than edit what was signed."""
    _validate(body)
    note = await latest_note(session, visit.appointment_id, for_update=True)
    if note is not None and note.status is NoteStatus.SIGNED:
        raise Conflict("the note is signed; amend it to make changes")
    if note is not None and note.provider_id != author_provider_id:
        raise PermissionDenied("only the note's author may edit its draft")
    before = _note_snapshot(note) if note else None
    if note is None:
        note = ClinicalNote(visit_id=visit.visit_id, appointment_id=visit.appointment_id,
                            patient_id=visit.patient_id, provider_id=author_provider_id,
                            version=1, status=NoteStatus.DRAFT)
        session.add(note)
    note.presenting_complaint = body.get("presenting_complaint", "")
    note.findings = body.get("findings", "")
    note.clinician_notes = body.get("clinician_notes", "")
    note.patient_summary = body.get("patient_summary", "")
    note.diagnoses = body.get("diagnoses", [])
    note.draft_prescriptions = body.get("prescriptions", [])
    note.draft_orders = body.get("orders", [])
    await session.flush()
    await _audit(audit, "clinical_note", note.id, "draft_saved", actor, before,
                 _note_snapshot(note))
    return note


async def sign(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    author_provider_id: uuid.UUID, actor: str, now: datetime,
) -> ClinicalNote:
    """Sign the current draft: it becomes immutable, and its prescriptions and orders become
    rows -- the active prescriptions and the open orders a patient's record shows."""
    note = await latest_note(session, appointment_id, for_update=True)
    if note is None:
        raise NotFound(f"appointment {appointment_id} has no note to sign")
    if note.status is NoteStatus.SIGNED:
        raise Conflict("the note is already signed")
    if note.provider_id != author_provider_id:
        raise PermissionDenied("only the note's author may sign it")
    if not note.diagnoses or not note.patient_summary.strip():
        raise Unprocessable("a note is signed with at least one diagnosis and a patient summary")
    note.status = NoteStatus.SIGNED
    note.signed_at = now
    note.signed_by = actor
    for item in note.draft_prescriptions:
        session.add(Prescription(
            note_id=note.id, patient_id=note.patient_id, medication=item["medication"],
            dose=item["dose"], frequency=item["frequency"], duration=item.get("duration", ""),
            instructions=item.get("instructions", ""), status=PrescriptionStatus.ACTIVE))
    orders = [TestOrder(note_id=note.id, patient_id=note.patient_id, ordered_by=note.provider_id,
                        test_code=item["test_code"], reason=item.get("reason", ""),
                        urgency=item.get("urgency", "routine"), status=OrderStatus.ORDERED)
              for item in note.draft_orders]
    session.add_all(orders)
    await session.flush()
    # Ids and counts only: clinical content never travels on Kafka (D-31).
    emit(session, "clinical.note_signed", key=str(note.patient_id), payload={
        "note_id": str(note.id), "visit_id": str(note.visit_id),
        "appointment_id": str(note.appointment_id), "patient_id": str(note.patient_id),
        "provider_id": str(note.provider_id), "version": note.version,
        "prescription_count": len(note.draft_prescriptions), "order_count": len(orders),
    })
    for order in orders:
        emit(session, "clinical.test_ordered", key=str(note.patient_id), payload={
            "order_id": str(order.id), "note_id": str(note.id),
            "patient_id": str(note.patient_id), "provider_id": str(note.provider_id),
            "urgency": order.urgency,
        })
    await _audit(audit, "clinical_note", note.id, "signed", actor, None, _note_snapshot(note))
    return note


async def amend(
    session: AsyncSession, audit: AuditLog, *, appointment_id: uuid.UUID,
    author_provider_id: uuid.UUID, actor: str,
) -> ClinicalNote:
    """Start a new draft version from the latest signed one (spec O-3: the author may amend
    at any time; the earlier version stays readable)."""
    note = await latest_note(session, appointment_id, for_update=True)
    if note is None or note.status is not NoteStatus.SIGNED:
        raise Conflict("only a signed note can be amended")
    if note.provider_id != author_provider_id:
        raise PermissionDenied("only the note's author may amend it")
    draft = ClinicalNote(
        visit_id=note.visit_id, appointment_id=note.appointment_id, patient_id=note.patient_id,
        provider_id=note.provider_id, version=note.version + 1, status=NoteStatus.DRAFT,
        presenting_complaint=note.presenting_complaint, findings=note.findings,
        clinician_notes=note.clinician_notes, patient_summary=note.patient_summary,
        diagnoses=list(note.diagnoses), draft_prescriptions=[], draft_orders=[],
    )
    session.add(draft)
    await session.flush()
    await _audit(audit, "clinical_note", draft.id, "amendment_started", actor,
                 _note_snapshot(note), _note_snapshot(draft))
    return draft


async def has_signed_note(session: AsyncSession, appointment_id: uuid.UUID) -> bool:
    return await latest_signed_note(session, appointment_id) is not None


@dataclass(frozen=True)
class Record:
    notes: list[ClinicalNote]
    prescriptions: list[Prescription]
    orders: list[TestOrder]
    documents: list[PatientDocument]


async def record(
    session: AsyncSession, audit: AuditLog, *, patient_id: uuid.UUID, actor: str,
    orders_only: bool = False,
) -> Record:
    """A patient's record: the latest signed version of each visit's note, prescriptions,
    orders and documents. `orders_only` is front desk's view (spec 9.6): order status and
    nothing clinical. Audited: who read which record."""
    await _audit(audit, "patient_record", patient_id, "record_viewed", actor)
    orders = list(await session.scalars(
        select(TestOrder).where(TestOrder.patient_id == patient_id)
        .order_by(TestOrder.created_at.desc())))
    if orders_only:
        return Record([], [], orders, [])
    latest = (select(ClinicalNote.visit_id, func.max(ClinicalNote.version).label("v"))
              .where(ClinicalNote.patient_id == patient_id,
                     ClinicalNote.status == NoteStatus.SIGNED)
              .group_by(ClinicalNote.visit_id).subquery())
    notes = list(await session.scalars(
        select(ClinicalNote).join(latest, (ClinicalNote.visit_id == latest.c.visit_id)
                                  & (ClinicalNote.version == latest.c.v))
        .order_by(ClinicalNote.signed_at.desc())))
    prescriptions = list(await session.scalars(
        select(Prescription).where(Prescription.patient_id == patient_id)
        .order_by(Prescription.created_at.desc())))
    documents = list(await session.scalars(
        select(PatientDocument).where(PatientDocument.patient_id == patient_id)
        .order_by(PatientDocument.created_at.desc())))
    return Record(notes, prescriptions, orders, documents)


async def add_document(
    session: AsyncSession, audit: AuditLog, *, patient_id: uuid.UUID, uploaded_by: str,
    filename: str, media_type: str, size: int, sha256: str, object_key: str,
    test_order_id: uuid.UUID | None,
) -> PatientDocument:
    """Record an uploaded file (its bytes are already stored), and attach it to the
    patient's open order if one is named: the order moves to result_uploaded."""
    if test_order_id is not None:
        order = await session.get(TestOrder, test_order_id, with_for_update=True)
        if order is None or order.patient_id != patient_id:
            raise NotFound(f"test order {test_order_id} does not exist for this patient")
    document = PatientDocument(
        patient_id=patient_id, uploaded_by=uploaded_by, filename=filename,
        media_type=media_type, size_bytes=size, sha256=sha256, object_key=object_key,
        test_order_id=test_order_id, extraction_status="pending")
    session.add(document)
    await session.flush()
    if test_order_id is not None and order.status is OrderStatus.ORDERED:
        order.status = OrderStatus.RESULT_UPLOADED
        order.result_document_id = document.id
    emit(session, "documents.uploaded", key=str(patient_id), payload={
        "document_id": str(document.id), "patient_id": str(patient_id),
        "test_order_id": str(test_order_id) if test_order_id else None,
    })
    await _audit(audit, "patient_document", document.id, "uploaded", uploaded_by, None,
                 {"filename": filename, "media_type": media_type, "size": size,
                  "test_order_id": str(test_order_id) if test_order_id else None})
    return document


async def review_order(
    session: AsyncSession, audit: AuditLog, *, order_id: uuid.UUID,
    reviewer_provider_id: uuid.UUID, actor: str, now: datetime,
) -> TestOrder:
    order = await session.get(TestOrder, order_id, with_for_update=True)
    if order is None:
        raise NotFound(f"test order {order_id} does not exist")
    if order.ordered_by != reviewer_provider_id:
        raise PermissionDenied("the ordering clinician reviews the result")
    if order.status is not OrderStatus.RESULT_UPLOADED:
        raise Conflict(f"the order is {order.status.value}; a result must be uploaded first")
    order.status = OrderStatus.REVIEWED
    order.reviewed_at = now
    await session.flush()
    await _audit(audit, "test_order", order.id, "reviewed", actor)
    return order


async def set_extraction(
    session: AsyncSession, document_id: uuid.UUID, *, status: str, text: str | None
) -> None:
    document = await session.get(PatientDocument, document_id)
    if document is not None:
        document.extraction_status = status
        document.extracted_text = text
