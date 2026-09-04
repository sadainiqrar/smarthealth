"""Provider management.

No FastAPI import — Week 2's Temporal activities call these directly. Mutations write
an audit document before returning, awaited so a Mongo outage fails loudly.

`register_provider` raises `Conflict` from inside a failed `flush()`, which leaves the
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
from app.modules.providers.models import Provider
from app.modules.providers.schemas import ProviderCreate, ProviderUpdate

ENTITY = "provider"

#: `Provider.license_number` is declared `unique=True` with no index, so Postgres
#: enforces it with a named UNIQUE CONSTRAINT -- unlike `Patient.mrn`, which is
#: `unique=True, index=True` and gets a unique INDEX instead. The two are genuinely
#: asymmetric; verified against `migrations/versions/0001_baseline.py` and confirmed
#: against the running database, which reports exactly this name.
LICENCE_UNIQUE_CONSTRAINT = "uq_providers_license_number"


def _snapshot(provider: Provider) -> dict[str, object]:
    """The audited shape of a provider: every column a mutation can change, including
    `user_id` -- a future account-linking flow that reuses this function needs that
    field in the trail, not just the identifying columns. It is a single place to
    redact from if a field ever needs to be excluded."""
    return {
        "first_name": provider.first_name,
        "last_name": provider.last_name,
        "specialty": provider.specialty,
        "license_number": provider.license_number,
        "is_active": provider.is_active,
        "user_id": str(provider.user_id) if provider.user_id is not None else None,
    }


async def register_provider(
    session: AsyncSession,
    audit: AuditLog,
    *,
    data: ProviderCreate,
    actor_user_id: str | None,
) -> Provider:
    provider = Provider(**data.model_dump())
    session.add(provider)
    try:
        await session.flush()
    except IntegrityError as exc:
        if violated_constraint(exc) == LICENCE_UNIQUE_CONSTRAINT:
            raise Conflict(
                f"a provider with licence number {data.license_number} already exists"
            ) from exc
        # Anything else -- a `user_id` collision, a foreign key, a check -- is not a
        # duplicate licence and must not be reported as one. Re-raise so it surfaces
        # as a 500 that gets investigated rather than a misleading 409.
        raise

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(provider.id),
            action="registered",
            actor_user_id=actor_user_id,
            after=_snapshot(provider),
        )
    )
    return provider


async def get_provider(session: AsyncSession, provider_id: uuid.UUID) -> Provider:
    """Fetch a provider by id.

    `session.get()` checks the identity map before the database. For one session per
    HTTP request (`expire_on_commit=False`) that is exactly right. A longer-lived
    session -- a Temporal activity or Celery task holding one across multiple calls --
    can get back a stale object on a second `get_provider` for the same id; call
    `session.expire(provider)` first, or pass `populate_existing=True`, when a fresh
    read matters. Week 2's booking flow reads `is_active`, so this matters there.
    """
    provider = await session.get(Provider, provider_id)
    if provider is None:
        raise NotFound(f"provider {provider_id} does not exist")
    return provider


async def list_providers(
    session: AsyncSession,
    *,
    page: PageParams,
    specialty: str | None = None,
    search: str | None = None,
) -> tuple[list[Provider], int]:
    """Return one page of providers and the total matching the same filter.

    `specialty` and `search` combine with AND: "cardiologists called Ada", not
    "cardiologists or anyone called Ada".
    """
    conditions = []
    if specialty:
        # Case-insensitive equality, not ILIKE: an unescaped ILIKE value lets a
        # client pass "%" and match every provider.
        conditions.append(func.lower(Provider.specialty) == specialty.lower())
    if search:
        # `%` and `_` are LIKE metacharacters. `Column.icontains(..., autoescape=True)`
        # escapes both (and the escape character itself) before wrapping the term in
        # wildcards, so a search for "a_b" cannot also match "axb". Use `icontains`,
        # not `contains`: `contains` compiles to a plain `LIKE`, which is
        # case-sensitive and would silently make "lovelace" stop matching "Lovelace".
        # `icontains` gives escaping and case-insensitivity together -- do not
        # "simplify" this back to `contains`. The same mistake was made and corrected
        # on the patients side.
        conditions.append(
            or_(
                Provider.first_name.icontains(search, autoescape=True),
                Provider.last_name.icontains(search, autoescape=True),
            )
        )

    # The same `conditions` go to both statements. Filter one and not the other and
    # `total` describes a different result set than `items`.
    total = await session.scalar(select(func.count()).select_from(Provider).where(*conditions))
    rows = await session.scalars(
        select(Provider)
        .where(*conditions)
        # `created_at` alone is not unique, and offset pagination over a non-unique
        # sort key skips and repeats rows across pages; `id` is the tiebreaker.
        .order_by(Provider.created_at.desc(), Provider.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)


async def update_provider(
    session: AsyncSession,
    audit: AuditLog,
    *,
    provider_id: uuid.UUID,
    data: ProviderUpdate,
    actor_user_id: str | None,
) -> Provider:
    provider = await get_provider(session, provider_id)
    before = _snapshot(provider)

    # No IntegrityError handling here, unlike register_provider: `ProviderUpdate`
    # cannot set `license_number` or `user_id`, the only columns with a uniqueness
    # constraint. If that changes -- account linking via `user_id`, or a deliberate
    # licence-correction operation -- this flush needs the same
    # try/except IntegrityError -> Conflict translation as register_provider, or the
    # violation surfaces as an unhandled 500 instead of a 409.
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    # `eager_defaults=True` on `Base` makes this flush emit `UPDATE ... RETURNING
    # updated_at`, so the expired-attribute reload that used to raise `MissingGreenlet`
    # while serialising the response never happens, and the value read back is the one
    # the `BEFORE UPDATE` trigger wrote (`clock_timestamp()`).
    await session.flush()

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(provider.id),
            action="profile_updated",
            actor_user_id=actor_user_id,
            before=before,
            after=_snapshot(provider),
        )
    )
    return provider
