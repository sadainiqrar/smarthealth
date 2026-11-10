"""GET /analytics/utilisation against real Postgres: a department's slots offered vs taken."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import UserRole
from app.modules.providers.models import Clinic, Department, Provider
from app.modules.scheduling.models import ProviderSlot, SlotStatus

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
async def factory(db_settings):
    engine = create_engine(db_settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_utilisation_is_booked_slots_of_those_offered_per_department(api, token_for,
                                                                           factory):
    """Referenced by tests/cases/an-010-department-utilisation.yaml."""
    tag = uuid.uuid4().hex[:8]
    async with factory() as session:
        clinic = Clinic(name=f"Utilisation {tag}", timezone="UTC")
        provider = Provider(first_name="Ada", last_name="Lovelace", specialty="Cardiology",
                            license_number=f"LIC-{tag}")
        session.add_all([clinic, provider])
        await session.flush()
        department = Department(clinic_id=clinic.id, name=f"Cardiology {tag}")
        session.add(department)
        await session.flush()
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        for i, status in enumerate((SlotStatus.BOOKED, SlotStatus.FREE, SlotStatus.FREE,
                                    SlotStatus.BLOCKED)):
            at = start + timedelta(hours=i)
            session.add(ProviderSlot(provider_id=provider.id, clinic_id=clinic.id,
                                     department_id=department.id, starts_at=at,
                                     ends_at=at + timedelta(minutes=30), status=status))
        await session.commit()
        ids = (clinic.id, department.id, provider.id)
    try:
        response = await api.get("/analytics/utilisation", params={
            "from": start.date().isoformat(), "to": (start + timedelta(days=1)).date().isoformat()},
            headers=token_for(UserRole.ADMIN))
        assert response.status_code == 200
        [row] = [r for r in response.json()["departments"] if r["clinic_id"] == str(ids[0])]
        # Four slots, one blocked (not offered): one taken of three offered.
        assert (row["department"], row["slots"], row["booked"]) == (f"Cardiology {tag}", 3, 1)
        assert row["utilisation"] == pytest.approx(1 / 3, abs=1e-3)
        refused = await api.get("/analytics/utilisation", headers=token_for(UserRole.PATIENT))
        assert refused.status_code == 403
    finally:
        async with factory() as session:
            await session.execute(delete(ProviderSlot).where(ProviderSlot.provider_id == ids[2]))
            await session.execute(delete(Department).where(Department.id == ids[1]))
            await session.execute(delete(Provider).where(Provider.id == ids[2]))
            await session.execute(delete(Clinic).where(Clinic.id == ids[0]))
            await session.commit()
