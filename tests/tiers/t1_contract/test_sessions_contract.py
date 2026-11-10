"""Refresh, logout, revocation, the published key and the booking rate limit, over HTTP.

The shared app's session store and rate limiter are the in-memory implementations here
(tests/conftest.py); the same flows against Redis are in
tests/tiers/t3_integration/test_sessions_api.py. Login itself needs the database and is
proven there too.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
import pytest

from app.modules.identity.models import UserRole
from tests.harness.auth import TEST_LIMITER, TEST_SESSIONS, bearer, issue_token

pytestmark = pytest.mark.contract


def _subject() -> str:
    return str(uuid.uuid4())


async def test_a_refresh_returns_a_new_pair(api_client):
    issued = TEST_SESSIONS.issue_now(user_id=_subject(), role="patient")
    response = await api_client.post(
        "/auth/refresh", json={"refresh_token": issued.refresh_token}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer" and body["expires_in"] > 0
    assert body["refresh_token"] != issued.refresh_token
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["sid"] == issued.session_id


async def test_replaying_a_spent_refresh_token_is_refused_and_ends_the_session(api_client):
    issued = TEST_SESSIONS.issue_now(user_id=_subject(), role="patient")
    first = await api_client.post("/auth/refresh", json={"refresh_token": issued.refresh_token})
    replay = await api_client.post("/auth/refresh", json={"refresh_token": issued.refresh_token})
    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.json()["error"] == "InvalidCredentials"
    newer = await api_client.post(
        "/auth/refresh", json={"refresh_token": first.json()["refresh_token"]}
    )
    assert newer.status_code == 401, "the replay must also have killed the newer token"


async def test_a_garbage_refresh_token_is_a_401_not_a_500(api_client):
    response = await api_client.post("/auth/refresh", json={"refresh_token": "no-dot-here"})
    assert response.status_code == 401


async def test_after_logout_the_same_access_token_is_refused(api_client):
    headers = bearer(issue_token(subject=_subject(), role=UserRole.PROVIDER))
    assert (await api_client.post("/auth/logout", headers=headers)).status_code == 200
    again = await api_client.post("/auth/logout", headers=headers)
    assert again.status_code == 401, "a token outlives its session only until the next request"


async def test_logout_all_ends_every_session_of_the_login(api_client):
    subject = _subject()
    first = bearer(issue_token(subject=subject, role=UserRole.ADMIN))
    second = bearer(issue_token(subject=subject, role=UserRole.ADMIN))
    other = bearer(issue_token(subject=_subject(), role=UserRole.ADMIN))
    response = await api_client.post("/auth/logout-all", headers=first)
    assert response.status_code == 200
    assert response.json() == {"sessions_ended": 2}
    assert (await api_client.post("/auth/logout", headers=second)).status_code == 401
    assert (await api_client.post("/auth/logout", headers=other)).status_code == 200


async def test_a_validly_signed_token_with_no_live_session_is_refused(api_client):
    token = issue_token(subject=_subject(), role=UserRole.ADMIN)
    session_id = jwt.decode(token, options={"verify_signature": False})["sid"]
    await TEST_SESSIONS.revoke(session_id)
    response = await api_client.post("/auth/logout", headers=bearer(token))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_the_published_key_verifies_an_issued_token(api_client):
    """What a Part B service does: fetch the JWKS once, verify tokens locally."""
    response = await api_client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    (key,) = response.json()["keys"]
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and "d" not in key
    token = issue_token(subject=_subject(), role=UserRole.PATIENT)
    assert jwt.get_unverified_header(token)["kid"] == key["kid"]
    claims = jwt.decode(
        token, jwt.PyJWK(key).key, algorithms=["EdDSA"], issuer="smarthealth-part-a"
    )
    assert claims["role"] == "patient"


async def test_the_booking_rate_limit_answers_429_with_retry_after(api_client):
    subject = _subject()
    for _ in range(10):  # booking_rate_limit_per_minute
        await TEST_LIMITER.hit(f"booking:{subject}", limit=10, window=timedelta(minutes=1))
    response = await api_client.post(
        "/appointments",
        json={"patient_id": str(uuid.uuid4()), "slot_id": str(uuid.uuid4())},
        headers=bearer(issue_token(subject=subject, role=UserRole.FRONT_DESK)),
    )
    assert response.status_code == 429
    assert response.json()["error"] == "RateLimited"
    assert 0 < int(response.headers["Retry-After"]) <= 60
