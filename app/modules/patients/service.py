"""Patient management.

No FastAPI import — Week 2's Temporal activities call these directly. Mutations write
an audit document before returning, awaited so a Mongo outage fails loudly.

`register_patient` raises `Conflict` from inside a failed `flush()`, which leaves the
session in the "pending rollback" state: any further statement on it raises
`PendingRollbackError` until someone rolls back. The HTTP path is covered — the
`get_session` dependency in `app.db.session` rolls back on any exception leaving the
request. A non-HTTP caller (a Temporal activity, a Celery task, a script) owns its own
session and must roll back before reusing it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound
from app.core.pagination import PageParams
from app.modules.patients.models import Patient
from app.modules.patients.schemas import PatientCreate, PatientUpdate

ENTITY = "patient"

#: The database enforces MRN uniqueness with a unique INDEX, not a unique constraint --
#: `Patient.mrn` is declared `unique=True, index=True`, so `0001_baseline.py` emits
#: `op.create_index(op.f('ix_patients_mrn'), 'patients', ['mrn'], unique=True)`.
#: Verified against the running database: asyncpg reports exactly this name.
MRN_UNIQUE_CONSTRAINT = "ix_patients_mrn"


def _violated_constraint(exc: IntegrityError) -> str | None:
    """The name of the constraint or unique index that rejected the write.

    `exc.orig` is *not* the asyncpg error: SQLAlchemy's asyncpg dialect re-raises it as
    its own `IntegrityError` shim, which carries only `sqlstate`/`pgcode`. The original
    `asyncpg.exceptions.UniqueViolationError` -- the object holding `constraint_name` --
    hangs off it as `__cause__`. Verified against the running database; matching on
    `exc.orig` alone always missed, which turned a duplicate MRN into a 500.
    """
    for candidate in (exc.orig.__cause__ if exc.orig is not None else None, exc.orig):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None


def _snapshot(patient: Patient) -> dict[str, object]:
    """The audited shape of a patient. Deliberately excludes nothing sensitive today,
    but is a single place to redact from if that changes."""
    return {
        "mrn": patient.mrn,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        "phone": patient.phone,
        "email": patient.email,
    }


async def register_patient(
    session: AsyncSession,
    audit: AuditLog,
    *,
    data: PatientCreate,
    actor_user_id: str | None,
) -> Patient:
    patient = Patient(**data.model_dump())
    session.add(patient)
    try:
        await session.flush()
    except IntegrityError as exc:
        if _violated_constraint(exc) == MRN_UNIQUE_CONSTRAINT:
            raise Conflict(f"a patient with MRN {data.mrn} already exists") from exc
        raise

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(patient.id),
            action="registered",
            actor_user_id=actor_user_id,
            after=_snapshot(patient),
        )
    )
    return patient


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise NotFound(f"patient {patient_id} does not exist")
    return patient


async def list_patients(
    session: AsyncSession, *, page: PageParams, search: str | None = None
) -> tuple[list[Patient], int]:
    """Return one page of patients and the total matching the same filter."""
    conditions = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                Patient.mrn.ilike(pattern),
                Patient.first_name.ilike(pattern),
                Patient.last_name.ilike(pattern),
            )
        )

    total = await session.scalar(select(func.count()).select_from(Patient).where(*conditions))
    rows = await session.scalars(
        select(Patient)
        .where(*conditions)
        .order_by(Patient.created_at.desc(), Patient.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)


async def update_patient(
    session: AsyncSession,
    audit: AuditLog,
    *,
    patient_id: uuid.UUID,
    data: PatientUpdate,
    actor_user_id: str | None,
) -> Patient:
    patient = await get_patient(session, patient_id)
    before = _snapshot(patient)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(patient, field, value)
    await session.flush()

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(patient.id),
            action="profile_updated",
            actor_user_id=actor_user_id,
            before=before,
            after=_snapshot(patient),
        )
    )
    return patient
