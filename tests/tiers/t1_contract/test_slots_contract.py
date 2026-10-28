"""GET /slots authorises and validates before any query runs, so no database is touched."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings()


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=str(uuid.uuid4()), role=role, settings=SETTINGS, now=datetime.now(UTC)
    )
    return {"Authorization": f"Bearer {token}"}


async def test_listing_slots_requires_a_token(api_client):
    response = await api_client.get(f"/slots?provider_id={uuid.uuid4()}")
    assert response.status_code == 401


async def test_provider_id_is_required(api_client):
    response = await api_client.get("/slots", headers=auth(UserRole.PATIENT))
    assert response.status_code == 422


async def test_a_malformed_provider_id_is_rejected(api_client):
    response = await api_client.get(
        "/slots?provider_id=not-a-uuid", headers=auth(UserRole.FRONT_DESK)
    )
    assert response.status_code == 422
