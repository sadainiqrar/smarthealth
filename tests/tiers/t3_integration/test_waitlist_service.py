"""The waitlist operations against real Postgres: adding, listing, the candidates for a
released slot, and idempotent fulfilment.

Tests say which mutation of `service.py` they exist to kill.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.clock import Clock
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.pagination import PageParams
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.providers.models import Department, Provider
from app.modules.scheduling import service
from app.modules.scheduling.models import (
    ProviderSlot,
    SlotStatus,
    WaitlistEntry,
    WaitlistStatus,
)
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import RecordingAudit, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]

ACTOR = "u-wl"


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    yield factory
    async with factory() as session:
        await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
    scheduling_helpers.CREATED.clear()
    await engine.dispose()


async def _join(factory, audit, b, *, provider=None, department=None, start=None, end=None):
    """One committed waitlist entry for `b`'s patient, by default covering `b`'s slot.
    Each is its own transaction, so `created_at` orders them."""
    async with factory() as session:
        slot = await session.get(ProviderSlot, b.slot_id)
        entry = await service.add_waitlist_entry(
            session, audit, patient_id=b.patient_id, provider_id=provider,
            department_id=department,
            window_start=start or slot.starts_at - timedelta(hours=1),
            window_end=end or slot.ends_at + timedelta(hours=1),
            actor_user_id=ACTOR,
        )
        await session.commit()
        return entry.id


async def _candidates(factory, slot_id, **kwargs):
    kwargs.setdefault("now", Clock().now())
    async with factory() as session:
        return await service.waitlist_candidates(session, slot_id=slot_id, **kwargs)


async def _department(factory, b, name) -> uuid.UUID:
    async with factory() as session:
        department = Department(clinic_id=b.clinic_id, name=name)
        session.add(department)
        await session.commit()
        return department.id


async def test_adding_an_entry_stores_it_active_and_audits_it(factory):
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    entry_id = await _join(factory, audit, b)
    async with factory() as session:
        entry = await session.get(WaitlistEntry, entry_id)
    assert entry.status is WaitlistStatus.ACTIVE and entry.patient_id == b.patient_id
    (event,) = audit.events
    assert (event.entity_type, event.action, event.actor_user_id) == (
        "waitlist_entry", "joined", ACTOR
    )


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-5)])
async def test_a_window_that_does_not_move_forward_is_unprocessable(factory, delta):
    """Kills: the `window_end <= window_start` guard removed or loosened to `<`."""
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
        slot = await session.get(ProviderSlot, b.slot_id)
        with pytest.raises(Unprocessable):
            await service.add_waitlist_entry(
                session, audit, patient_id=b.patient_id, provider_id=None,
                department_id=None, window_start=slot.starts_at,
                window_end=slot.starts_at + delta, actor_user_id=ACTOR,
            )
    assert audit.events == []


async def test_an_unknown_patient_is_not_found(factory):
    async with factory() as session:
        b = await seed_bookable(session)
        slot = await session.get(ProviderSlot, b.slot_id)
        with pytest.raises(NotFound):
            await service.add_waitlist_entry(
                session, RecordingAudit(), patient_id=uuid.uuid4(), provider_id=None,
                department_id=None, window_start=slot.starts_at,
                window_end=slot.ends_at, actor_user_id=ACTOR,
            )


async def test_listing_filters_by_patient_and_pages(factory):
    audit = RecordingAudit()
    async with factory() as session:
        a = await seed_bookable(session)
        c = await seed_bookable(session, starts_in=timedelta(days=3))
    first = await _join(factory, audit, a)
    second = await _join(factory, audit, a)
    await _join(factory, audit, c)
    async with factory() as session:
        items, total = await service.list_waitlist(
            session, patient_id=a.patient_id, page=PageParams(limit=1, offset=0)
        )
        rest, _ = await service.list_waitlist(
            session, patient_id=a.patient_id, page=PageParams(limit=1, offset=1)
        )
    assert total == 2
    assert [e.id for e in items + rest] == [first, second]  # oldest first


async def test_candidates_are_active_entries_whose_window_holds_the_slot_oldest_first(factory):
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
        starts = (await session.get(ProviderSlot, b.slot_id)).starts_at
    first = await _join(factory, audit, b)
    second = await _join(factory, audit, b)
    async with factory() as session:
        # Make the later-inserted entry the older one, so only an ORDER BY on `created_at`
        # (not insertion order) puts it first. Kills: `order_by` dropped.
        older = await session.get(WaitlistEntry, second)
        older.created_at = (await session.get(WaitlistEntry, first)).created_at - timedelta(
            minutes=1
        )
        await session.commit()
    await _join(  # window ends before the slot: never a candidate
        factory, audit, b, start=starts - timedelta(days=2), end=starts - timedelta(hours=1)
    )
    await _join(  # window starts after the slot
        factory, audit, b, start=starts + timedelta(hours=1), end=starts + timedelta(days=1)
    )

    assert await _candidates(factory, b.slot_id) == [
        (second, b.patient_id), (first, b.patient_id),
    ]


async def test_the_window_includes_its_start_and_excludes_its_end(factory):
    """`window_start <= starts_at < window_end`: an offer at exactly the end would be for
    a slot after the patient's window. Kills: either bound flipped."""
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
        starts = (await session.get(ProviderSlot, b.slot_id)).starts_at
    at_start = await _join(factory, audit, b, start=starts, end=starts + timedelta(hours=1))
    await _join(factory, audit, b, start=starts - timedelta(hours=1), end=starts)
    assert [e for e, _ in await _candidates(factory, b.slot_id)] == [at_start]


async def test_an_entry_that_is_not_active_is_not_a_candidate(factory):
    """Kills: the `status = 'active'` predicate dropped from `waitlist_candidates`."""
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    done = await _join(factory, audit, b)
    waiting = await _join(factory, audit, b)
    async with factory() as session:
        await service.fulfil_waitlist_entry(
            session, audit, entry_id=done, actor_user_id=ACTOR
        )
        await session.commit()
    assert [e for e, _ in await _candidates(factory, b.slot_id)] == [waiting]


async def test_provider_and_department_filters_apply_only_when_set(factory):
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
        other = Provider(
            first_name="Alan", last_name="Turing", specialty="cardiology",
            license_number=f"LIC-{uuid.uuid4().hex[:10]}",
        )
        session.add(other)
        await session.commit()
        other_provider = other.id
    department = await _department(factory, b, "Cardiology")
    elsewhere = await _department(factory, b, "Neurology")
    async with factory() as session:
        (await session.get(ProviderSlot, b.slot_id)).department_id = department
        await session.commit()

    anyone = await _join(factory, audit, b)
    this_provider = await _join(factory, audit, b, provider=b.provider_id)
    await _join(factory, audit, b, provider=other_provider)
    this_department = await _join(factory, audit, b, department=department)
    await _join(factory, audit, b, department=elsewhere)

    got = [e for e, _ in await _candidates(factory, b.slot_id)]

    assert got == [anyone, this_provider, this_department]
    async with factory() as session:  # the foreign provider is not purged with `b`
        await session.delete(await session.get(Provider, other_provider))
        await session.commit()


async def test_candidates_are_limited(factory):
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    ids = [await _join(factory, audit, b) for _ in range(4)]
    assert [e for e, _ in await _candidates(factory, b.slot_id)] == ids[:3]
    assert len(await _candidates(factory, b.slot_id, limit=1)) == 1


async def test_candidates_for_an_unknown_slot_are_empty(factory):
    assert await _candidates(factory, uuid.uuid4()) == []


@pytest.mark.parametrize("status", [SlotStatus.HELD, SlotStatus.BOOKED, SlotStatus.BLOCKED])
async def test_a_slot_that_is_not_free_has_no_candidates(factory, status):
    """M-1: the slot was taken again before the promotion read it. Offering it would only
    make every candidate's booking fail in turn. Kills: the `free` check dropped."""
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    await _join(factory, audit, b)
    async with factory() as session:
        (await session.get(ProviderSlot, b.slot_id)).status = status
        await session.commit()
    assert await _candidates(factory, b.slot_id) == []


@pytest.mark.parametrize("after", [timedelta(0), timedelta(minutes=5)], ids=["at", "after"])
async def test_a_slot_that_has_started_has_no_candidates(factory, after):
    """M-1: a cancel after the slot began (front desk closing an unattended appointment)
    frees a slot nobody can book (R-3). Kills: the `starts_at > now` check dropped or
    loosened to `>=`."""
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
        starts = (await session.get(ProviderSlot, b.slot_id)).starts_at
    entry = await _join(factory, audit, b)
    assert [e for e, _ in await _candidates(factory, b.slot_id)] == [entry]
    assert await _candidates(factory, b.slot_id, now=starts + after) == []


async def test_fulfilling_an_entry_is_idempotent(factory):
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    entry_id = await _join(factory, audit, b)
    for _ in range(2):
        async with factory() as session:
            await service.fulfil_waitlist_entry(
                session, audit, entry_id=entry_id, actor_user_id=ACTOR
            )
            await session.commit()
    async with factory() as session:
        entry = await session.get(WaitlistEntry, entry_id)
    assert entry.status is WaitlistStatus.FULFILLED
    assert [e.action for e in audit.events] == ["joined", "fulfilled"]  # once, not twice


async def test_fulfilling_an_unknown_entry_is_not_found(factory):
    async with factory() as session:
        with pytest.raises(NotFound):
            await service.fulfil_waitlist_entry(
                session, RecordingAudit(), entry_id=uuid.uuid4(), actor_user_id=ACTOR
            )


async def test_fulfilling_an_entry_that_was_cancelled_is_a_conflict(factory):
    audit = RecordingAudit()
    async with factory() as session:
        b = await seed_bookable(session)
    entry_id = await _join(factory, audit, b)
    async with factory() as session:
        (await session.get(WaitlistEntry, entry_id)).status = WaitlistStatus.CANCELLED
        await session.commit()
    async with factory() as session:
        with pytest.raises(Conflict):
            await service.fulfil_waitlist_entry(
                session, audit, entry_id=entry_id, actor_user_id=ACTOR
            )
