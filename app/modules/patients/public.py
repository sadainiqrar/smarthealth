"""The patients module's published interface (PRD D-15).

The only file another module may import from `patients`, and only from the call sites
declared in tests/meta/test_import_boundaries.py. It returns ids and raises domain
errors, never ORM objects: a caller that receives a `Patient` instance is coupled to
this module's schema, which is exactly what the boundary exists to prevent.
"""

from __future__ import annotations

import uuid

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
