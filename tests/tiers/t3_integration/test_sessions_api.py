"""Login, refresh, logout and deactivation against real Postgres and Redis."""

import uuid
from datetime import UTC, datetime

import pytest

from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PASSWORD = "correct horse battery staple"


async def _user(session, *, role: UserRole = UserRole.ADMIN) -> str:
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    session.add(User(email=email, password_hash=hash_password(PASSWORD), role=role,
                     is_active=True))
    await session.commit()
    return email


async def _login(api, email: str, password: str = PASSWORD):
    return await api.post("/auth/login", json={"email": email, "password": password})


async def test_login_refresh_and_logout_round_trip(api, db_session):
    """Referenced by tests/cases/aut-003-a-session-ends-when-it-is-logged-out.yaml."""
    email = await _user(db_session)
    login = await _login(api, email)
    assert login.status_code == 200
    tokens = login.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await api.get("/providers", headers=headers)).status_code == 200

    refreshed = await api.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    new_headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (await api.get("/providers", headers=new_headers)).status_code == 200

    assert (await api.post("/auth/logout", headers=new_headers)).status_code == 200
    # Both access tokens named the one session; both are dead at once, not at expiry.
    assert (await api.get("/providers", headers=headers)).status_code == 401
    assert (await api.get("/providers", headers=new_headers)).status_code == 401
    spent = await api.post(
        "/auth/refresh", json={"refresh_token": refreshed.json()["refresh_token"]}
    )
    assert spent.status_code == 401


async def test_logout_all_ends_sessions_on_every_device(api, db_session):
    email = await _user(db_session)
    laptop = (await _login(api, email)).json()
    phone = (await _login(api, email)).json()
    laptop_headers = {"Authorization": f"Bearer {laptop['access_token']}"}
    phone_headers = {"Authorization": f"Bearer {phone['access_token']}"}
    response = await api.post("/auth/logout-all", headers=laptop_headers)
    assert response.json() == {"sessions_ended": 2}
    assert (await api.get("/providers", headers=phone_headers)).status_code == 401


async def test_deactivating_a_login_ends_its_live_sessions(api, db_session, db_settings):
    """Referenced by tests/cases/aut-004-deactivating-a-login-ends-its-sessions.yaml.

    The Week 1 gap: a deactivated user kept access until the token expired."""
    from app.db.redis import create_redis_client
    from app.modules.identity.service import deactivate_user
    from app.modules.identity.sessions import RedisSessionStore

    email = await _user(db_session)
    tokens = (await _login(api, email)).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await api.get("/providers", headers=headers)).status_code == 200

    redis = create_redis_client(db_settings)
    try:
        user, revoked = await deactivate_user(
            db_session, email=email, sessions=RedisSessionStore(redis, db_settings),
            now=datetime.now(UTC),
        )
    finally:
        await redis.aclose()
    assert revoked == 1
    assert (await api.get("/providers", headers=headers)).status_code == 401
    # Announced, in the deactivation's own transaction, for the Part B services -- which
    # verify tokens by signature alone and learn of a revocation only this way.
    from sqlalchemy import select

    from app.events.models import OutboxEvent

    (event,) = (
        await db_session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.topic == "identity.sessions_revoked",
                OutboxEvent.key == str(user.id),
            )
        )
    ).all()
    assert event.payload["reason"] == "deactivated" and event.payload["session_id"] is None
    assert (await _login(api, email)).status_code == 401


async def test_an_address_is_locked_after_five_failures_and_says_nothing_new(api, db_session):
    """Referenced by tests/cases/aut-005-a-guessed-address-locks-without-revealing-it.yaml."""
    email = await _user(db_session)
    unknown = f"nobody-{uuid.uuid4().hex[:8]}@example.com"
    for _ in range(5):
        assert (await _login(api, email, "wrong")).status_code == 401
        assert (await _login(api, unknown, "wrong")).status_code == 401
    locked_right = await _login(api, email)
    locked_unknown = await _login(api, unknown, "wrong")
    assert locked_right.status_code == locked_unknown.status_code == 401
    assert locked_right.json() == locked_unknown.json()
