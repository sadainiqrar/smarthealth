"""Login against a real database.

The T1 contract lane proves the request/response shape with no infrastructure; this
proves the credential check itself, against a real `users` row with a real argon2
hash.
"""

import uuid

import pytest

from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def _user(session, *, email: str, password: str, active: bool = True) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password),
        role=UserRole.ADMIN,
        is_active=active,
    )
    session.add(user)
    await session.commit()
    return user


async def test_login_issues_a_usable_token(api, db_session):
    """Referenced by tests/cases/aut-001-login-issues-a-token.yaml."""
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    await _user(db_session, email=email, password="correct horse battery staple")

    response = await api.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    # The token must actually open a protected route.
    listing = await api.get(
        "/providers", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert listing.status_code == 200


async def test_a_wrong_password_and_an_unknown_email_are_indistinguishable(api, db_session):
    """Different messages would tell an attacker which addresses are registered."""
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    await _user(db_session, email=email, password="right")

    wrong_password = await api.post(
        "/auth/login", json={"email": email, "password": "wrong"}
    )
    unknown_email = await api.post(
        "/auth/login",
        json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com", "password": "x"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_an_inactive_account_cannot_log_in(api, db_session):
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    await _user(db_session, email=email, password="right", active=False)

    response = await api.post(
        "/auth/login", json={"email": email, "password": "right"}
    )
    assert response.status_code == 401
