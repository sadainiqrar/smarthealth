"""Operational command-line entry point.

`python -m app.cli create-user` is the only way to put a login into the system
outside of a test fixture, which is what makes a running instance demonstrable to a
human reviewer. This module must never import `app.main` or any router: it is an
operational entry point, not part of the request path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError

from app.db.constraints import violated_constraint
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.settings import Settings

#: The unique index backing `users.email` (see migrations/versions/0001_baseline.py:
#: `op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)`).
#: A unique *index*, not a named `UniqueConstraint` -- confirmed empirically by
#: provoking a real duplicate-email insert against the live database and reading
#: `violated_constraint()` off the resulting `IntegrityError`.
USERS_EMAIL_CONSTRAINT = "ix_users_email"


async def create_user(*, settings: Settings, email: str, password: str, role: str) -> None:
    """Insert a usable `users` row, or exit(1) politely on a bad input.

    Builds and disposes its own engine so it can run standalone, outside any request
    lifecycle. Never prints the password.
    """
    try:
        parsed_role = UserRole(role)
    except ValueError:
        print(f"error: '{role}' is not a valid role. Choose from: "
              f"{', '.join(member.value for member in UserRole)}", file=sys.stderr)
        raise SystemExit(1) from None

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            user = User(
                email=email,
                password_hash=hash_password(password),
                role=parsed_role,
                is_active=True,
            )
            session.add(user)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if violated_constraint(exc) == USERS_EMAIL_CONSTRAINT:
                    print(
                        f"error: a user with email '{email}' already exists",
                        file=sys.stderr,
                    )
                    raise SystemExit(1) from None
                raise
            print(f"created user id={user.id} role={user.role.value}")
    finally:
        await engine.dispose()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user_parser = subparsers.add_parser(
        "create-user", help="Create a login (users row) that can authenticate."
    )
    create_user_parser.add_argument("--email", required=True)
    create_user_parser.add_argument("--password", required=True)
    create_user_parser.add_argument(
        "--role", required=True, choices=[member.value for member in UserRole]
    )

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "create-user":
        asyncio.run(
            create_user(
                settings=Settings(),
                email=args.email,
                password=args.password,
                role=args.role,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    main()
