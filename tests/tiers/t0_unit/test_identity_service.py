"""Unit tests for `authenticate`, with a fake session so no database is needed."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.core.errors import InvalidCredentials
from app.modules.identity import service
from app.modules.identity.models import UserRole
from app.settings import Settings

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 3, 10, 30, tzinfo=UTC)
MESSAGE = "email or password is incorrect"


class _StubUser:
    """The slice of `User` that `authenticate` reads."""

    def __init__(self, *, is_active: bool = True) -> None:
        self.id = "11111111-1111-1111-1111-111111111111"
        self.role = UserRole.PATIENT
        self.is_active = is_active
        self.password_hash = "$argon2id$not-a-real-hash"


class _FakeSession:
    """Returns whatever user the test wants; `authenticate` never touches SQL."""

    def __init__(self, user: _StubUser | None) -> None:
        self._user = user

    async def scalar(self, _statement):
        return self._user


async def _login(user: _StubUser | None, *, password: str = "whatever"):
    return await service.authenticate(
        _FakeSession(user),
        email="someone@example.test",
        password=password,
        settings=Settings(),
        now=NOW,
    )


async def test_verify_password_runs_even_when_no_user_matches():
    """The behavioural pin on the timing fix.

    argon2 is deliberately slow, so skipping the verification for an unknown address
    makes that address answer ~55ms faster than a registered one — the identical
    error message hides the leak from a reader but not from a stopwatch. Asserting
    the call happened is stable in CI in a way that asserting elapsed time is not.
    """
    with patch.object(service, "verify_password", return_value=False) as verify:
        with pytest.raises(InvalidCredentials):
            await _login(None)

    verify.assert_called_once()
    assert verify.call_args.args[1] == service._DUMMY_PASSWORD_HASH


async def test_verify_password_runs_even_when_the_account_is_inactive():
    """Same leak, second door: an inactive account must not answer faster either."""
    with patch.object(service, "verify_password", return_value=True) as verify:
        with pytest.raises(InvalidCredentials):
            await _login(_StubUser(is_active=False))

    verify.assert_called_once()


async def test_verify_password_is_called_exactly_once_on_success():
    """Guards the other direction: the dummy hash must not add a second argon2 pass."""
    with patch.object(service, "verify_password", return_value=True) as verify:
        token, expires_in = await _login(_StubUser())

    verify.assert_called_once()
    assert token
    assert expires_in == Settings().jwt_expiry_minutes * 60


@pytest.mark.parametrize(
    ("user", "matches"),
    [
        (None, False),
        (_StubUser(is_active=False), True),
        (_StubUser(), False),
    ],
    ids=["absent-user", "inactive-user", "wrong-password"],
)
async def test_every_failure_path_raises_the_identical_message(user, matches):
    """Absent, inactive, and wrong-password are indistinguishable to the caller."""
    with patch.object(service, "verify_password", return_value=matches):
        with pytest.raises(InvalidCredentials) as raised:
            await _login(user)

    assert str(raised.value) == MESSAGE


def test_the_dummy_hash_is_a_real_argon2_hash():
    """A placeholder string would be rejected by the parser in microseconds, which
    would put the timing gap straight back."""
    assert service._DUMMY_PASSWORD_HASH.startswith("$argon2")
