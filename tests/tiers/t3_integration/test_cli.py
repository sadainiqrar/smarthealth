"""`python -m app.cli create-user` against a real database.

This is the only way to create an account outside of a test fixture, which is what
makes the running system demonstrable to a human reviewer rather than provable only
to the test suite.
"""

import uuid

import pytest
from sqlalchemy import select

from app.cli import create_user
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import verify_password

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_create_user_inserts_a_usable_account(db_settings, db_session):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.com"
    await create_user(
        settings=db_settings, email=email, password="a strong secret", role="admin"
    )

    user = await db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.role is UserRole.ADMIN
    assert user.is_active is True
    assert verify_password("a strong secret", user.password_hash)


async def test_create_user_refuses_a_duplicate_email(db_settings):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.com"
    await create_user(settings=db_settings, email=email, password="x1", role="admin")

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x2", role="admin")
    assert exit_info.value.code == 1


async def test_create_user_refuses_an_invalid_role(db_settings):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.com"

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x1", role="wizard")
    assert exit_info.value.code == 1


async def test_create_user_writes_no_row_for_an_address_that_cannot_log_in(
    db_settings, db_session
):
    """The regression this guards: the CLI used to accept `.test` and write a row
    whose owner could never authenticate, because `LoginRequest.email` is an
    `EmailStr` and `email-validator` refuses reserved TLDs. T0 proves the check
    fires before any database work; this proves the table really stays empty.
    """
    email = f"cli-{uuid.uuid4().hex[:8]}@medinova.test"

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x1", role="admin")
    assert exit_info.value.code == 1

    assert await db_session.scalar(select(User).where(User.email == email)) is None
