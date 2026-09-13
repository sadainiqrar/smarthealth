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

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError

from app.db.constraints import violated_constraint
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.seed import USERS as SEED_USERS
from app.seed import clear as seed_clear
from app.seed import seed as seed_data
from app.settings import Settings

#: The unique index backing `users.email` (see migrations/versions/0001_baseline.py:
#: `op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)`).
#: A unique *index*, not a named `UniqueConstraint` -- confirmed empirically by
#: provoking a real duplicate-email insert against the live database and reading
#: `violated_constraint()` off the resulting `IntegrityError`.
USERS_EMAIL_CONSTRAINT = "ix_users_email"

#: The *same* validator `LoginRequest.email` uses, so the CLI cannot accept an address
#: the login endpoint will later reject. `email-validator` refuses the reserved TLDs
#: (`.test`, `.invalid`, `localhost`) as well as malformed addresses, so without this
#: check `create-user --email admin@medinova.test` writes a row whose owner can never
#: authenticate: the account looks created and is silently useless. Validate through
#: pydantic rather than a hand-rolled regex — a second implementation would drift from
#: `LoginRequest` and reintroduce exactly this disagreement.
_EMAIL = TypeAdapter(EmailStr)


async def create_user(*, settings: Settings, email: str, password: str, role: str) -> None:
    """Insert a usable `users` row, or exit(1) politely on a bad input.

    Builds and disposes its own engine so it can run standalone, outside any request
    lifecycle. Never prints the password.
    """
    try:
        _EMAIL.validate_python(email)
    except ValidationError:
        print(
            f"error: '{email}' is not an address that can log in. The login endpoint "
            f"validates the same way and would reject it.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

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


async def run_seed(
    *, settings: Settings, password: str, patient_count: int, clear: bool
) -> None:
    """Fill an empty database with a demonstrable clinic network, or empty it again.

    Owns the transaction, the same way a router does for a request: `app.seed` only
    flushes. One commit means a half-seeded database is not a state that can exist --
    a partial failure rolls the whole thing back rather than leaving providers with
    no slots.
    """
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            if clear:
                summary = await seed_clear(session, patient_count=patient_count)
                action = "removed"
            else:
                summary = await seed_data(
                    session, password=password, patient_count=patient_count
                )
                action = "created"
            await session.commit()

        print(f"{action}:")
        for line in summary.as_lines():
            print(line)
        if not clear:
            print("\nlogins (all share the password you supplied):")
            for email, role in SEED_USERS:
                print(f"  {email:34} {role.value}")
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

    seed_parser = subparsers.add_parser(
        "seed",
        help="Insert a demonstrable clinic network. Idempotent; --clear undoes it.",
        description=(
            "Seeds clinics, departments, providers, patients, one login per role, and "
            "a two-week window of bookable slots. Deliberately seeds no appointments, "
            "visits or waitlist entries -- those are workflow outputs that Week 2 "
            "produces, and hand-writing them would fabricate state no workflow created."
        ),
    )
    # Required rather than defaulted. A well-known credential written into a database
    # by a tool that never asked is how a demo database becomes an incident.
    seed_parser.add_argument(
        "--password",
        required=True,
        help="Password for every seeded login. Required; there is no default.",
    )
    seed_parser.add_argument(
        "--patients",
        type=int,
        default=50,
        help="How many patient records to create (default: 50).",
    )
    seed_parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove the seeded rows instead of creating them.",
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
    elif args.command == "seed":
        if args.patients < 0:
            print("error: --patients cannot be negative", file=sys.stderr)
            raise SystemExit(1)
        asyncio.run(
            run_seed(
                settings=Settings(),
                password=args.password,
                patient_count=args.patients,
                clear=args.clear,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    main()
