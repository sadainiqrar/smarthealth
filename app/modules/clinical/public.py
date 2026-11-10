"""What other modules may use from the clinical record (the D-15 pattern).

Scheduling's complete endpoint asks here whether the visit's note is signed: a visit
completes only with its record (spec 9.6).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clinical import service


async def has_signed_note(session: AsyncSession, appointment_id: uuid.UUID) -> bool:
    return await service.has_signed_note(session, appointment_id)
