from datetime import UTC, datetime, timedelta

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import (
    InvalidToken,
    TokenClaims,
    create_access_token,
    decode_access_token,
    generate_signing_key,
    hash_password,
    public_jwk,
    verify_password,
)
from app.settings import Settings

pytestmark = pytest.mark.unit

SETTINGS = Settings(jwt_private_key=generate_signing_key(), access_token_minutes=30)
SID = "session-1"
#: Anchored to the real clock, not a fixed date. `decode_access_token` validates
#: `exp` against wall-clock time, so a hardcoded NOW makes every token-validity test
#: depend on what time of day the suite happens to run — and eventually fail forever.
NOW = datetime.now(UTC)


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
        session_id=SID,
        settings=SETTINGS,
        now=NOW,
    )
    claims = decode_access_token(token, settings=SETTINGS)
    assert isinstance(claims, TokenClaims)
    assert claims.subject == "11111111-1111-1111-1111-111111111111"
    assert claims.role is UserRole.PROVIDER
    assert claims.session_id == SID


def test_an_expired_token_is_rejected():
    token = create_access_token(
        subject="u1", role=UserRole.PATIENT, session_id=SID, settings=SETTINGS,
        now=NOW - timedelta(hours=2),
    )
    with pytest.raises(InvalidToken, match="expired"):
        decode_access_token(token, settings=SETTINGS)


def test_a_token_signed_with_another_key_is_rejected():
    """Accepting a foreign signature would let anyone mint an admin token."""
    token = create_access_token(
        subject="u1", role=UserRole.ADMIN, session_id=SID,
        settings=Settings(jwt_private_key=generate_signing_key()), now=NOW,
    )
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings=SETTINGS)


def test_a_tampered_token_is_rejected():
    token = create_access_token(
        subject="u1", role=UserRole.PATIENT, session_id=SID, settings=SETTINGS, now=NOW
    )
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}XY.{signature}"
    with pytest.raises(InvalidToken):
        decode_access_token(tampered, settings=SETTINGS)


def _signed(payload: dict) -> str:
    import jwt
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(SETTINGS.jwt_private_key.encode(), None)
    return jwt.encode(payload, key, algorithm="EdDSA")


def test_an_unknown_role_in_a_token_is_rejected():
    token = _signed({
        "sub": "u1", "role": "superuser", "sid": SID, "iss": SETTINGS.jwt_issuer,
        "exp": NOW + timedelta(hours=1),
    })
    with pytest.raises(InvalidToken, match="role"):
        decode_access_token(token, settings=SETTINGS)


def test_a_token_naming_no_session_is_rejected():
    """Every token must name its session, or revoking a session would not reach it."""
    token = _signed({
        "sub": "u1", "role": "admin", "iss": SETTINGS.jwt_issuer,
        "exp": NOW + timedelta(hours=1),
    })
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings=SETTINGS)


def test_a_token_from_another_issuer_is_rejected():
    token = _signed({
        "sub": "u1", "role": "admin", "sid": SID, "iss": "someone-else",
        "exp": NOW + timedelta(hours=1),
    })
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings=SETTINGS)


def test_an_hs256_token_is_rejected_even_when_keyed_with_the_public_key():
    """The algorithm-confusion attack: an HMAC token keyed with the published public
    key. Only EdDSA is accepted, so it fails before any key is compared."""
    import jwt

    token = jwt.encode(
        {"sub": "u1", "role": "admin", "sid": SID, "iss": SETTINGS.jwt_issuer,
         "exp": NOW + timedelta(hours=1)},
        public_jwk(SETTINGS)["x"] + "-padding-to-32-bytes-for-hmac-key", algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings=SETTINGS)


def test_the_token_header_names_the_published_key():
    import jwt

    token = create_access_token(
        subject="u1", role=UserRole.ADMIN, session_id=SID, settings=SETTINGS, now=NOW
    )
    assert jwt.get_unverified_header(token)["kid"] == public_jwk(SETTINGS)["kid"]


def test_the_public_jwk_is_an_ed25519_verification_key_with_no_private_part():
    jwk = public_jwk(SETTINGS)
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519" and jwk["alg"] == "EdDSA"
    assert "d" not in jwk
    assert public_jwk(Settings(jwt_private_key=generate_signing_key()))["kid"] != jwk["kid"]


def test_user_roles_are_exactly_the_four_the_requirements_name():
    assert {role.value for role in UserRole} == {
        "patient", "provider", "front_desk", "admin"
    }


def test_a_naive_now_is_rejected():
    """`datetime.timestamp()` reads a naive value as local time, silently shifting
    the token's lifetime by the machine's UTC offset."""
    with pytest.raises(ValueError, match="timezone-aware"):
        create_access_token(
            subject="u1", role=UserRole.PATIENT, session_id=SID, settings=SETTINGS,
            now=datetime(2026, 9, 2, 12, 0),
        )


def test_an_unparseable_stored_hash_is_a_failed_login_not_a_crash():
    """A corrupted column must not turn a login into a 500 that leaks a stack trace."""
    assert verify_password("anything", "not-a-hash") is False


def test_an_empty_stored_hash_is_a_failed_login():
    assert verify_password("anything", "") is False
