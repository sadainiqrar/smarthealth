"""GET /patients/me authorises by role before any query runs, so no database is touched."""

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


async def test_me_requires_a_token(api_client):
    assert (await api_client.get("/patients/me")).status_code == 401


@pytest.mark.parametrize("role", [UserRole.FRONT_DESK, UserRole.PROVIDER, UserRole.ADMIN])
async def test_me_is_for_patients_only(api_client, role):
    """403, not 422: proves /me is routed before /{patient_id}."""
    assert (await api_client.get("/patients/me", headers=auth(role))).status_code == 403
