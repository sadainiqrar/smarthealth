"""The providers module's published interface (PRD D-15, R-24).

The only file another module may import from `providers`, and only from the call sites
declared in tests/meta/test_import_boundaries.py. Like `patients.public`, it returns
plain values and raises domain errors, never ORM objects.

Both functions read the `is_active` column with a query, never through `session.get`:
the identity map can hold a `Provider` loaded before a deactivation committed, and a
booking activity must see the current answer.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, Unprocessable
from app.modules.providers.models import Provider


async def _active_flag(session: AsyncSession, provider_id: uuid.UUID) -> bool | None:
    """`is_active`, or None when no such provider exists."""
    return await session.scalar(select(Provider.is_active).where(Provider.id == provider_id))


async def ensure_bookable_provider(session: AsyncSession, provider_id: uuid.UUID) -> None:
    """Raise unless the provider exists and is active.

    Unknown is `NotFound`, as `ensure_bookable_patient` answers for an unknown patient;
    a deactivated provider exists, so it is `Unprocessable` -- the request is well formed
    and names a real provider, who takes no new bookings. Both are domain errors, so a
    booking activity fails permanently and the workflow compensates.
    """
    active = await _active_flag(session, provider_id)
    if active is None:
        raise NotFound(f"provider {provider_id} does not exist")
    if not active:
        raise Unprocessable(f"provider {provider_id} is not active")


async def is_active_provider(session: AsyncSession, provider_id: uuid.UUID) -> bool:
    """True only for an existing, active provider -- for reads that filter rather than
    refuse (`GET /slots` lists nothing for an inactive or unknown provider)."""
    return bool(await _active_flag(session, provider_id))
