"""The published provider interface, against real Postgres (PRD R-24)."""

import uuid

import pytest
from sqlalchemy import update

from app.core.errors import NotFound, Unprocessable
from app.modules.providers import public
from app.modules.providers.models import Provider

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def _provider(db_session, *, is_active: bool = True) -> uuid.UUID:
    provider = Provider(
        first_name="Ada", last_name="Lovelace", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}", is_active=is_active,
    )
    db_session.add(provider)
    await db_session.flush()
    return provider.id


async def test_an_active_provider_is_bookable(db_session):
    provider_id = await _provider(db_session)
    await public.ensure_bookable_provider(db_session, provider_id)
    assert await public.is_active_provider(db_session, provider_id) is True


async def test_an_inactive_provider_is_unprocessable(db_session):
    provider_id = await _provider(db_session, is_active=False)
    with pytest.raises(Unprocessable, match="not active"):
        await public.ensure_bookable_provider(db_session, provider_id)
    assert await public.is_active_provider(db_session, provider_id) is False


async def test_an_unknown_provider_is_not_found(db_session):
    with pytest.raises(NotFound):
        await public.ensure_bookable_provider(db_session, uuid.uuid4())
    assert await public.is_active_provider(db_session, uuid.uuid4()) is False


async def test_a_deactivation_in_the_same_session_is_seen(db_session):
    """An UPDATE the session does not synchronise -- as a deactivation committed by
    another session would look -- leaves a loaded `Provider` stale in the identity map;
    the interface must read the column, not the cached object (the warning on
    `get_provider`). Kills: `_active_flag` rewritten as `session.get(...).is_active`."""
    provider_id = await _provider(db_session)
    # Held in a name: the identity map is weak, so a discarded result could be
    # collected and reloaded fresh, and the test would prove nothing.
    loaded = await db_session.get(Provider, provider_id)
    assert loaded.is_active
    await db_session.execute(
        update(Provider)
        .where(Provider.id == provider_id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(Unprocessable):
        await public.ensure_bookable_provider(db_session, provider_id)
    assert loaded.is_active, "the cached object is still stale: the check read past it"
