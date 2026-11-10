"""The patients module's published interface (PRD D-15).

The only file another module may import from `patients`, and only from the call sites
declared in tests/meta/test_import_boundaries.py. It returns ids and raises domain
errors, never ORM objects: a caller that receives a `Patient` instance is coupled to
this module's schema, which is exactly what the boundary exists to prevent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.patients.models import Patient


async def ensure_bookable_patient(session: AsyncSession, patient_id: uuid.UUID) -> None:
    """Raise `NotFound` unless the patient exists.

    "Bookable" is existence today -- patients carry no active flag. When eligibility grows
    rules (an inactive record, an unpaid balance), they land here and every booking path
    picks them up without changing.
    """
    found = await session.scalar(select(exists().where(Patient.id == patient_id)))
    if not found:
        raise NotFound(f"patient {patient_id} does not exist")


async def patient_id_for_user(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """The patient record linked to a login, or None for a user with no patient record."""
    return await session.scalar(select(Patient.id).where(Patient.user_id == user_id))


@dataclass(frozen=True)
class PatientContact:
    """How to reach a patient: what notifications needs, and nothing more of the record."""

    first_name: str
    email: str | None
    phone: str | None


async def contact_for(session: AsyncSession, patient_id: uuid.UUID) -> PatientContact | None:
    """The patient's name and contact details, or None for an unknown id. Read by query,
    like the other functions here, so a stale identity-map object cannot answer."""
    row = (
        await session.execute(
            select(Patient.first_name, Patient.email, Patient.phone).where(Patient.id == patient_id)
        )
    ).one_or_none()
    return PatientContact(row.first_name, row.email, row.phone) if row else None


async def count_patients(session: AsyncSession) -> int:
    """How many patients are registered: the Total Patients metric (PART-A-FR-5)."""
    from sqlalchemy import func

    return int(await session.scalar(select(func.count()).select_from(Patient)) or 0)


async def names_for_users(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The full name of the patient record linked to each login that has one: how a user
    directory shows a person rather than an address (the assistant's usage page)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(Patient.user_id, Patient.first_name, Patient.last_name)
        .where(Patient.user_id.in_(user_ids)))
    return {r.user_id: f"{r.first_name} {r.last_name}" for r in rows}
