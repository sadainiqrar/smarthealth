from datetime import UTC, datetime, timedelta

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import (
    InvalidToken,
    TokenClaims,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.settings import Settings

pytestmark = pytest.mark.unit

SETTINGS = Settings(jwt_secret="unit-test-secret", jwt_expiry_minutes=30)
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_password_hash_round_trips():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)


def test_password_hash_is_not_the_password():
    hashed = hash_password("hunter2")
    assert "hunter2" not in hashed


def test_wrong_password_is_rejected():
    assert not verify_password("wrong", hash_password("right"))


def test_the_same_password_hashes_differently_each_time():
    """Identical hashes would mean no salt, letting one leak crack every account."""
    assert hash_password("same") != hash_password("same")


def test_token_round_trips_with_subject_and_role():
    token = create_access_token(
        subject="11111111-1111-1111-1111-111111111111",
        role=UserRole.PROVIDER,
        settings=SETTINGS,
        now=NOW,
    )
    claims = decode_access_token(token, settings=SETTINGS)
    assert isinstance(claims, TokenClaims)
    assert claims.subject == "11111111-1111-1111-1111-111111111111"
    assert claims.role is UserRole.PROVIDER


def test_an_expired_token_is_rejected():
    token = create_access_token(
        subject="u1", role=UserRole.PATIENT, settings=SETTINGS,
        now=NOW - timedelta(hours=2),
    )
    with pytest.raises(InvalidToken, match="expired"):
        decode_access_token(token, settings=SETTINGS)


def test_a_token_signed_with_another_secret_is_rejected():
    """Accepting a foreign signature would let anyone mint an admin token."""
    token = create_access_token(
        subject="u1", role=UserRole.ADMIN,
        settings=Settings(jwt_secret="attacker-secret"), now=NOW,
    )
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings=SETTINGS)


def test_a_tampered_token_is_rejected():
    token = create_access_token(
        subject="u1", role=UserRole.PATIENT, settings=SETTINGS, now=NOW
    )
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}XY.{signature}"
    with pytest.raises(InvalidToken):
        decode_access_token(tampered, settings=SETTINGS)


def test_an_unknown_role_in_a_token_is_rejected():
    import jwt

    token = jwt.encode(
        {"sub": "u1", "role": "superuser", "exp": NOW + timedelta(hours=1)},
        SETTINGS.jwt_secret, algorithm=SETTINGS.jwt_algorithm,
    )
    with pytest.raises(InvalidToken, match="role"):
        decode_access_token(token, settings=SETTINGS)


def test_user_roles_are_exactly_the_four_the_requirements_name():
    assert {role.value for role in UserRole} == {
        "patient", "provider", "front_desk", "admin"
    }
