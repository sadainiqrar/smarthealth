"""GET /slots against real Postgres: only free, future slots of the provider, in order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.scheduling.models import ProviderSlot, SlotStatus
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import add_slot, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    session_factory = create_session_factory(engine)
    try:
        yield session_factory
    finally:
        async with session_factory() as session:
            await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
        scheduling_helpers.CREATED.clear()
        await engine.dispose()


async def test_only_free_future_slots_of_the_provider_are_listed(api, token_for, factory):
    async with factory() as session:
        b = await seed_bookable(session)  # free, tomorrow
        later = await add_slot(session, b, starts_in=timedelta(days=2))
        past = await add_slot(session, b, starts_in=timedelta(hours=-2))
        taken = await add_slot(session, b, starts_in=timedelta(days=3))
        await session.execute(
            update(ProviderSlot).where(ProviderSlot.id == taken).values(status=SlotStatus.BOOKED)
        )
        await session.commit()
        other = await seed_bookable(session)  # another provider's slot

    response = await api.get(
        f"/slots?provider_id={b.provider_id}", headers=token_for(UserRole.PATIENT)
    )

    assert response.status_code == 200
    body = response.json()
    ids = [item["id"] for item in body["items"]]
    assert ids == [str(b.slot_id), str(later)]
    assert str(past) not in ids and str(taken) not in ids and str(other.slot_id) not in ids
    assert body["total"] == 2


async def test_the_window_narrows_the_list(api, token_for, factory):
    async with factory() as session:
        b = await seed_bookable(session)  # tomorrow
        await add_slot(session, b, starts_in=timedelta(days=5))
    window_end = datetime.now(UTC) + timedelta(days=2)

    response = await api.get(
        "/slots",
        params={"provider_id": str(b.provider_id), "to": window_end.isoformat()},
        headers=token_for(UserRole.FRONT_DESK),
    )

    assert [item["id"] for item in response.json()["items"]] == [str(b.slot_id)]
