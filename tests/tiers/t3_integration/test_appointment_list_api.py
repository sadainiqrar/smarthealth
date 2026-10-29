"""GET /appointments against real Postgres: a patient's own list, staff filters, ordering
and totals. Appointment rows are written directly -- the list reads state, and the
workflows that produce it are proven elsewhere."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.patients.models import Patient
from app.modules.scheduling.models import Appointment, AppointmentStatus, ProviderSlot
from tests.tiers.t3_integration import scheduling_helpers
from tests.tiers.t3_integration.scheduling_helpers import Bookable, add_slot, seed_bookable

pytestmark = [pytest.mark.integration, pytest.mark.docker]

DAY = timedelta(days=1)


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    session_factory = create_session_factory(engine)
    try:
        yield session_factory
    finally:
        async with session_factory() as session:
            await scheduling_helpers.purge(session, scheduling_helpers.CREATED)
            await session.execute(delete(User).where(User.email.like("list-%@example.com")))
            await session.commit()
        scheduling_helpers.CREATED.clear()
        await engine.dispose()


def _appointment(
    b: Bookable, status: AppointmentStatus, slot_id: uuid.UUID | None
) -> Appointment:
    return Appointment(
        id=uuid.uuid4(), patient_id=b.patient_id, provider_id=b.provider_id,
        clinic_id=b.clinic_id, slot_id=slot_id, status=status,
        confirmed_at=datetime.now(UTC) if status is AppointmentStatus.CONFIRMED else None,
    )


async def _starts_at(session, slot_id: uuid.UUID) -> datetime:
    return await session.scalar(select(ProviderSlot.starts_at).where(ProviderSlot.id == slot_id))


@pytest.fixture
async def world(factory):
    """Patient `b` (logged in as `user_id`) has three appointments with one provider --
    confirmed tomorrow, cancelled in three days, and a `pending` one with no slot yet.
    Another patient with another provider has one of their own."""
    async with factory() as session:
        b = await seed_bookable(session)  # slot tomorrow
        o = await seed_bookable(session)
        later = await add_slot(session, b, starts_in=3 * DAY)
        user = User(
            email=f"list-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("pw-not-used"), role=UserRole.PATIENT,
        )
        session.add(user)
        await session.flush()
        (await session.get(Patient, b.patient_id)).user_id = user.id
        confirmed = _appointment(b, AppointmentStatus.CONFIRMED, b.slot_id)
        cancelled = _appointment(b, AppointmentStatus.CANCELLED, later)
        slotless = _appointment(b, AppointmentStatus.PENDING, None)
        theirs = _appointment(o, AppointmentStatus.CONFIRMED, o.slot_id)
        session.add_all([confirmed, cancelled, slotless, theirs])
        await session.commit()
        return SimpleWorld(
            b=b, o=o, user_id=user.id, confirmed=confirmed.id, cancelled=cancelled.id,
            slotless=slotless.id, theirs=theirs.id, later_slot=later,
            first_start=await _starts_at(session, b.slot_id),
            later_start=await _starts_at(session, later),
        )


class SimpleWorld:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def _ids(response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


async def test_a_patient_sees_only_their_own_newest_slot_first_and_slotless_last(
    api, token_for, world
):
    headers = token_for(UserRole.PATIENT, subject=str(world.user_id))
    response = await api.get("/appointments", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert _ids(response) == [str(world.cancelled), str(world.confirmed), str(world.slotless)]
    assert str(world.theirs) not in _ids(response)
    assert body["total"] == 3
    starts = [item["starts_at"] for item in body["items"]]
    assert datetime.fromisoformat(starts[0]) == world.later_start
    assert datetime.fromisoformat(starts[1]) == world.first_start
    assert starts[2] is None


async def test_a_patient_cannot_list_another_patient_by_naming_them(api, token_for, world):
    headers = token_for(UserRole.PATIENT, subject=str(world.user_id))
    response = await api.get(f"/appointments?patient_id={world.o.patient_id}", headers=headers)
    assert response.status_code == 403


async def test_a_patient_login_with_no_record_is_a_404(api, token_for, world):
    response = await api.get("/appointments", headers=token_for(UserRole.PATIENT))
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


async def test_staff_filter_by_patient_provider_and_status(api, token_for, world):
    staff = token_for(UserRole.FRONT_DESK)

    by_patient = await api.get(f"/appointments?patient_id={world.b.patient_id}", headers=staff)
    assert by_patient.json()["total"] == 3

    by_provider = await api.get(f"/appointments?provider_id={world.o.provider_id}", headers=staff)
    assert _ids(by_provider) == [str(world.theirs)]
    assert by_provider.json()["total"] == 1

    by_status = await api.get(
        f"/appointments?provider_id={world.b.provider_id}&status=cancelled", headers=staff
    )
    assert _ids(by_status) == [str(world.cancelled)]
    assert by_status.json()["total"] == 1


async def test_the_slot_window_bounds_starts_at_and_drops_slotless_rows(api, token_for, world):
    staff = token_for(UserRole.ADMIN)
    # (httpx replaces a URL's query string when `params` is given, so carry it all there.)
    provider = {"provider_id": str(world.b.provider_id)}
    # `from` is inclusive, `to` exclusive; neither admits a row with no slot.
    after = await api.get(
        "/appointments", params={**provider, "from": world.later_start.isoformat()},
        headers=staff,
    )
    assert _ids(after) == [str(world.cancelled)] and after.json()["total"] == 1

    before = await api.get(
        "/appointments", params={**provider, "to": world.later_start.isoformat()},
        headers=staff,
    )
    assert _ids(before) == [str(world.confirmed)] and before.json()["total"] == 1

    unbounded = await api.get("/appointments", params=provider, headers=staff)
    assert unbounded.json()["total"] == 3


async def test_pages_carry_the_full_total_and_do_not_overlap(api, token_for, world):
    staff = token_for(UserRole.FRONT_DESK)
    base = f"/appointments?patient_id={world.b.patient_id}&limit=2"
    first = await api.get(base, headers=staff)
    second = await api.get(base + "&offset=2", headers=staff)
    assert first.json()["total"] == second.json()["total"] == 3
    assert _ids(first) + _ids(second) == [
        str(world.cancelled), str(world.confirmed), str(world.slotless)
    ]


async def test_the_single_read_carries_the_same_slot_time(api, token_for, world):
    headers = token_for(UserRole.PATIENT, subject=str(world.user_id))
    one = await api.get(f"/appointments/{world.confirmed}", headers=headers)
    assert datetime.fromisoformat(one.json()["starts_at"]) == world.first_start
    none = await api.get(f"/appointments/{world.slotless}", headers=headers)
    assert none.json()["starts_at"] is None


async def test_pages_do_not_overlap_when_created_at_ties(api, token_for, factory):
    """`Appointment.id` is the final `ORDER BY` key: slot-less rows sort together
    (`starts_at` NULL) and rows written in one transaction share `now()`, so without a
    unique tiebreak the page boundary is arbitrary and a row can repeat or vanish."""
    async with factory() as session:
        b = await seed_bookable(session)
        first = _appointment(b, AppointmentStatus.PENDING, None)
        second = _appointment(b, AppointmentStatus.PENDING, None)
        session.add_all([first, second])
        await session.commit()
        created = (
            await session.execute(
                select(Appointment.created_at).where(Appointment.patient_id == b.patient_id)
            )
        ).scalars().all()
    assert len(set(created)) == 1, "precondition: identical created_at"

    staff = token_for(UserRole.FRONT_DESK)
    url = f"/appointments?patient_id={b.patient_id}&limit=1"
    page_one = await api.get(f"{url}&offset=0", headers=staff)
    page_two = await api.get(f"{url}&offset=1", headers=staff)

    assert page_one.status_code == page_two.status_code == 200
    ids = _ids(page_one) + _ids(page_two)
    assert len(ids) == 2 and len(set(ids)) == 2
    assert set(ids) == {str(first.id), str(second.id)}
    assert ids == sorted(ids), "ties fall back to ascending id"
