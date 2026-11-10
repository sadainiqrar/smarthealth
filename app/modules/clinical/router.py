"""The clinical record's routes, and who may do what with them (spec section 9.6).

Access is role **and** relationship, decided here before the service is called:

| | note, diagnoses, summary | clinician notes | prescriptions | orders | documents |
| patient (own record)     | signed only | no  | yes | yes | yes |
| provider who treats them | yes         | yes | yes | yes | yes |
| provider who does not    | no -- 404   |     |     |     |     |
| front desk               | no          | no  | no  | status only | upload only |
| admin                    | no (the audit trail is theirs, not the record) |

"Treats them" is a care relationship (`scheduling.public.has_care_relationship`): a
completed visit or a live appointment. A provider without one gets 404, not 403, so record
ids cannot be probed. Writing a note is the appointment's own provider's. This closes PRD
R-14 for the record; the visit endpoints check the same in scheduling's router.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.patients.public as patients
import app.modules.providers.public as providers
import app.modules.scheduling.public as scheduling
from app.api.deps import get_audit_log, get_session
from app.core.audit import AuditLog
from app.core.clock import Clock, get_clock
from app.core.errors import NotFound, PermissionDenied, Unprocessable
from app.modules.clinical import service
from app.modules.clinical.catalogue import CATALOGUE
from app.modules.clinical.models import ClinicalNote, OrderStatus, TestOrder
from app.modules.clinical.schemas import (
    DocumentRead,
    DocumentText,
    NoteIn,
    NoteRead,
    OrderRead,
    PrescriptionRead,
    RecordRead,
)
from app.modules.clinical.storage import DocumentStore
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims

router = APIRouter(tags=["clinical"])

#: Content types a report may have, recognised by their first bytes, not their name.
SIGNATURES = {b"%PDF": "application/pdf", b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
RECORD_READERS = (UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK)
UPLOADERS = (UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK)


def get_document_store(request: Request) -> DocumentStore:
    return request.app.state.documents


def get_extractor(request: Request):
    """Enqueues text extraction for a document id (Celery). Overridden in the contract tier."""
    return request.app.state.enqueue_extraction


async def _provider_of(session: AsyncSession, claims: TokenClaims) -> uuid.UUID:
    provider_id = await providers.provider_id_for_user(session, uuid.UUID(claims.subject))
    if provider_id is None:
        raise PermissionDenied("this login is not linked to a provider record")
    return provider_id


async def _visit(session: AsyncSession, appointment_id: uuid.UUID) -> service.VisitFacts:
    facts = await scheduling.visit_facts(session, appointment_id)
    if facts is None:
        raise NotFound(f"appointment {appointment_id} has no visit yet")
    return service.VisitFacts(facts[0], appointment_id, facts[1], facts[2])


async def _author(session: AsyncSession, claims: TokenClaims, appointment_id: uuid.UUID):
    """The visit, and the caller as its author: the appointment's own provider only."""
    visit = await _visit(session, appointment_id)
    provider_id = await _provider_of(session, claims)
    if provider_id != visit.provider_id:
        raise NotFound(f"appointment {appointment_id} has no visit yet")
    return visit, provider_id


async def _may_read(session: AsyncSession, claims: TokenClaims, patient_id: uuid.UUID) -> str:
    """How the caller may read this patient's record: "patient", "clinician" or "orders"."""
    if claims.role is UserRole.PATIENT:
        if await patients.patient_id_for_user(session, uuid.UUID(claims.subject)) != patient_id:
            raise NotFound("no such record")
        return "patient"
    if claims.role is UserRole.PROVIDER:
        provider_id = await _provider_of(session, claims)
        if not await scheduling.has_care_relationship(session, provider_id, patient_id):
            raise NotFound("no such record")
        return "clinician"
    if claims.role is UserRole.FRONT_DESK:
        return "orders"
    raise PermissionDenied("clinical records are not readable by this role")


def _note_read(note: ClinicalNote, view: str) -> NoteRead:
    return NoteRead(
        id=note.id, appointment_id=note.appointment_id, patient_id=note.patient_id,
        provider_id=note.provider_id, version=note.version, status=note.status.value,
        presenting_complaint=note.presenting_complaint, findings=note.findings,
        clinician_notes=note.clinician_notes if view == "clinician" else None,
        patient_summary=note.patient_summary, diagnoses=note.diagnoses,
        prescriptions=note.draft_prescriptions, orders=note.draft_orders,
        signed_at=note.signed_at,
    )


def _order_read(order: TestOrder, view: str) -> OrderRead:
    test = CATALOGUE[order.test_code]
    return OrderRead(
        id=order.id, test_code=order.test_code, test_name=test.name,
        prep_source_id=test.prep_source_id, urgency=order.urgency,
        status=order.status.value, created_at=order.created_at,
        reason=None if view == "orders" else order.reason,
        result_document_id=None if view == "orders" else order.result_document_id,
    )


# --- the note -----------------------------------------------------------------------------


@router.put("/appointments/{appointment_id}/note", response_model=NoteRead)
async def save_note(
    appointment_id: uuid.UUID, body: NoteIn,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(UserRole.PROVIDER)),
) -> NoteRead:
    """Create or update the visit's draft note. Saved as the clinician goes; signing is
    separate, and a signed note is amended, never edited."""
    visit, provider_id = await _author(session, claims, appointment_id)
    note = await service.save_draft(session, audit, visit=visit, author_provider_id=provider_id,
                                    body=body.model_dump(), actor=claims.subject)
    await session.commit()
    return _note_read(note, "clinician")


@router.post("/appointments/{appointment_id}/note/sign", response_model=NoteRead)
async def sign_note(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    clock: Clock = Depends(get_clock),
    claims: TokenClaims = Depends(require_role(UserRole.PROVIDER)),
) -> NoteRead:
    _, provider_id = await _author(session, claims, appointment_id)
    note = await service.sign(session, audit, appointment_id=appointment_id,
                              author_provider_id=provider_id, actor=claims.subject,
                              now=clock.now())
    await session.commit()
    return _note_read(note, "clinician")


@router.post("/appointments/{appointment_id}/note/amend", response_model=NoteRead,
             status_code=status.HTTP_201_CREATED)
async def amend_note(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(UserRole.PROVIDER)),
) -> NoteRead:
    _, provider_id = await _author(session, claims, appointment_id)
    note = await service.amend(session, audit, appointment_id=appointment_id,
                               author_provider_id=provider_id, actor=claims.subject)
    await session.commit()
    return _note_read(note, "clinician")


@router.get("/appointments/{appointment_id}/note", response_model=NoteRead)
async def read_note(
    appointment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    claims: TokenClaims = Depends(require_role(UserRole.PATIENT, UserRole.PROVIDER)),
) -> NoteRead:
    """The latest version a reader may see: a clinician who treats the patient sees the
    draft too; the patient sees the latest signed version, without clinician-only notes."""
    parties = await scheduling.appointment_parties(session, appointment_id)
    if parties is None:
        raise NotFound(f"appointment {appointment_id} does not exist")
    view = await _may_read(session, claims, parties[0])
    note = (await service.latest_note(session, appointment_id) if view == "clinician"
            else await service.latest_signed_note(session, appointment_id))
    if note is None:
        raise NotFound(f"appointment {appointment_id} has no note")
    return _note_read(note, view)


# --- the record --------------------------------------------------------------------------


async def _record(session, audit, claims, patient_id: uuid.UUID) -> RecordRead:
    view = await _may_read(session, claims, patient_id)
    record = await service.record(session, audit, patient_id=patient_id, actor=claims.subject,
                                  orders_only=view == "orders")
    return RecordRead(
        patient_id=patient_id,
        notes=[_note_read(n, view) for n in record.notes],
        prescriptions=[PrescriptionRead.model_validate(p) for p in record.prescriptions],
        orders=[_order_read(o, view) for o in record.orders],
        documents=[DocumentRead.model_validate(d) for d in record.documents],
    )


@router.get("/patients/me/record", response_model=RecordRead)
async def my_record(
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(UserRole.PATIENT)),
) -> RecordRead:
    patient_id = await patients.patient_id_for_user(session, uuid.UUID(claims.subject))
    if patient_id is None:
        raise NotFound("this login is not linked to a patient record")
    return await _record(session, audit, claims, patient_id)


@router.get("/patients/{patient_id}/record", response_model=RecordRead)
async def patient_record(
    patient_id: uuid.UUID,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*RECORD_READERS)),
) -> RecordRead:
    return await _record(session, audit, claims, patient_id)


# --- documents and results ---------------------------------------------------------------


@router.post("/patients/{patient_id}/documents", response_model=DocumentRead,
             status_code=status.HTTP_201_CREATED)
async def upload_document(
    patient_id: uuid.UUID, request: Request,
    filename: str = Query(min_length=1, max_length=255),
    test_order_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    store: DocumentStore = Depends(get_document_store),
    enqueue=Depends(get_extractor),
    claims: TokenClaims = Depends(require_role(*UPLOADERS)),
) -> DocumentRead:
    """Upload a report: the request body is the file (PDF, PNG or JPEG, up to 10 MB). The
    patient for themselves, front desk and the treating provider on their behalf. The type
    is read from the bytes, not trusted from the name. Text is extracted afterwards."""
    if claims.role is not UserRole.FRONT_DESK:
        await _may_read(session, claims, patient_id)
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise Unprocessable(f"a document is 1 byte to {MAX_UPLOAD_BYTES} bytes")
    media_type = next((t for sig, t in SIGNATURES.items() if data.startswith(sig)), None)
    if media_type is None:
        raise Unprocessable("only PDF, PNG and JPEG documents are accepted")
    digest = hashlib.sha256(data).hexdigest()
    key = f"patients/{patient_id}/{uuid.uuid4()}"
    # The object first, the row last: a failure leaves an orphan object, never a row that
    # names a file that is not there.
    await store.put(key, data, media_type)
    document = await service.add_document(
        session, audit, patient_id=patient_id, uploaded_by=claims.subject, filename=filename,
        media_type=media_type, size=len(data), sha256=digest, object_key=key,
        test_order_id=test_order_id)
    await session.commit()
    enqueue(str(document.id))
    return DocumentRead.model_validate(document)


@router.get("/patients/{patient_id}/documents/{document_id}", response_model=DocumentText)
async def read_document(
    patient_id: uuid.UUID, document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(UserRole.PATIENT, UserRole.PROVIDER)),
) -> DocumentText:
    """A document and its extracted text, for the patient and the treating clinician --
    what the assistant reads to explain or summarise a report (spec 9.1)."""
    await _may_read(session, claims, patient_id)
    record = await service.record(session, audit, patient_id=patient_id, actor=claims.subject)
    document = next((d for d in record.documents if d.id == document_id), None)
    if document is None:
        raise NotFound(f"document {document_id} does not exist")
    order = next((o for o in record.orders if o.id == document.test_order_id), None)
    return DocumentText(**DocumentRead.model_validate(document).model_dump(),
                        extracted_text=document.extracted_text,
                        reviewed=bool(order and order.status is OrderStatus.REVIEWED))


@router.post("/test-orders/{order_id}/review", response_model=OrderRead)
async def review_result(
    order_id: uuid.UUID,
    session: AsyncSession = Depends(get_session), audit: AuditLog = Depends(get_audit_log),
    clock: Clock = Depends(get_clock),
    claims: TokenClaims = Depends(require_role(UserRole.PROVIDER)),
) -> OrderRead:
    """The ordering clinician marks an uploaded result reviewed."""
    order = await service.review_order(session, audit, order_id=order_id,
                                       reviewer_provider_id=await _provider_of(session, claims),
                                       actor=claims.subject, now=clock.now())
    await session.commit()
    return _order_read(order, "clinician")

