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
from app.db.constraints import violated_constraint
from app.modules.patients.models import Patient
from app.modules.patients.schemas import PatientCreate, PatientUpdate

ENTITY = "patient"

#: The database enforces MRN uniqueness with a unique INDEX, not a unique constraint --
#: `Patient.mrn` is declared `unique=True, index=True`, so `0001_baseline.py` emits
#: `op.create_index(op.f('ix_patients_mrn'), 'patients', ['mrn'], unique=True)`.
#: Verified against the running database: asyncpg reports exactly this name.
MRN_UNIQUE_CONSTRAINT = "ix_patients_mrn"


def _snapshot(patient: Patient) -> dict[str, object]:
    """The audited shape of a patient: every column a mutation can change, including
    `user_id` -- a future account-linking flow that reuses this function needs that
    field in the trail, not just the identifying and contact columns. It is a single
    place to redact from if a field ever needs to be excluded."""
    return {
        "mrn": patient.mrn,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else None,
        "phone": patient.phone,
        "email": patient.email,
        "user_id": str(patient.user_id) if patient.user_id is not None else None,
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
        if violated_constraint(exc) == MRN_UNIQUE_CONSTRAINT:
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
    """Fetch a patient by id.

    `session.get()` checks the identity map before the database. For one session per
    HTTP request (`expire_on_commit=False`) that is exactly right. A longer-lived
    session -- a Temporal activity or Celery task holding one across multiple calls --
    can get back a stale object on a second `get_patient` for the same id; call
    `session.expire(patient)` first, or pass `populate_existing=True`, when a fresh
    read matters.
    """
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
        # `%` and `_` are LIKE metacharacters. `Column.icontains(..., autoescape=True)`
        # escapes both (and the escape character itself) before wrapping the term in
        # wildcards, so a search for "a_b" cannot also match "axb" -- see the
        # before/after comparison run against the live database, documented in the
        # commit that introduced this fix. Use `icontains`, not `contains`: `contains`
        # compiles to a plain `LIKE`, which is case-sensitive and would silently make
        # "bloggs" stop matching "Bloggs". `icontains` compiles to
        # `lower(col) LIKE lower(term) ESCAPE '/'`, so escaping and case-insensitivity
        # both hold -- do not "simplify" this back to `contains`.
        conditions.append(
            or_(
                Patient.mrn.icontains(search, autoescape=True),
                Patient.first_name.icontains(search, autoescape=True),
                Patient.last_name.icontains(search, autoescape=True),
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

    # No IntegrityError handling here, unlike register_patient: `PatientUpdate` cannot
    # set `mrn` or `user_id`, the only columns with a uniqueness constraint, and `email`
    # has none. If that changes -- a unique index added to `email`, or `PatientUpdate`
    # extended to allow account linking via `user_id` -- this flush needs the same
    # try/except IntegrityError -> Conflict translation as register_patient, or the
    # violation surfaces as an unhandled 500 instead of a 409.
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
