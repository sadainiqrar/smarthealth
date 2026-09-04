"""`create-user` rejects an address that could never log in.

T0, not T3: the check runs before `create_user` builds an engine, so no database is
needed to prove either that a bad address is refused or that no row could have been
written. The sentinel engine below makes "never reached the database" an assertion
rather than an assumption — a T3 test could only observe the absence of a row, which
is the weaker claim. The happy path still needs a real database and stays in T3.
"""

import pytest

from app import cli

pytestmark = pytest.mark.unit


class _EngineWasBuilt(Exception):
    """Raised by the sentinel to mark that `create_user` got past validation."""


@pytest.fixture
def sentinel_engine(monkeypatch):
    """Replace `create_engine` so reaching the database is loud, not silent."""

    def _explode(settings):
        raise _EngineWasBuilt

    monkeypatch.setattr(cli, "create_engine", _explode)


@pytest.mark.parametrize(
    "email",
    [
        # `.test` is a reserved special-use TLD; `email-validator` refuses it, and so
        # therefore does `LoginRequest.email`. This is the address that motivated the
        # fix: the CLI used to write the row and the login then 422'd forever.
        "admin@medinova.test",
        "admin@example.invalid",
        "admin@localhost",
        "not-an-email",
        "",
    ],
)
async def test_an_address_that_cannot_log_in_is_refused_before_any_database_work(
    email, sentinel_engine
):
    with pytest.raises(SystemExit) as exit_info:
        await cli.create_user(settings=None, email=email, password="s3cret", role="admin")
    assert exit_info.value.code == 1


async def test_the_refusal_names_the_address_on_stderr_and_never_the_password(
    capsys, sentinel_engine
):
    with pytest.raises(SystemExit):
        await cli.create_user(
            settings=None, email="admin@medinova.test", password="s3cret", role="admin"
        )

    captured = capsys.readouterr()
    assert "admin@medinova.test" in captured.err
    assert captured.out == ""
    assert "s3cret" not in captured.err


async def test_a_valid_address_passes_validation_and_proceeds(sentinel_engine):
    """The guard must not break the normal path: an `example.com` address gets all
    the way to building an engine, which is where the database work begins."""
    with pytest.raises(_EngineWasBuilt):
        await cli.create_user(
            settings=None, email="admin@example.com", password="s3cret", role="admin"
        )
