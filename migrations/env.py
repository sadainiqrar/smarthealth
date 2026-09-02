"""Alembic environment.

The URL comes from application settings rather than alembic.ini so there is exactly
one place a DSN is defined. `SMARTHEALTH_POSTGRES_DB` selects the target database,
which is how the test harness points migrations at its per-run database.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.db.all_models import Base  # noqa: F401  - imports every model module
from app.settings import Settings

config = context.config
target_metadata = Base.metadata

#: Objects Alembic cannot diff reliably, excluded from autogenerate comparison.
#:
#: The Enum CHECKs: SQLAlchemy's metadata renders them as an unexpanded post-compile
#: placeholder (`role IN (__[POSTCOMPILE_param_1])`) while Postgres stores
#: `(role)::text = ANY (ARRAY[...])`. Alembic cannot match the two textual forms and
#: proposes dropping them on every run - verified to happen even for a database built
#: by `Base.metadata.create_all()` with no migration involved.
#:
#: The partial index: a `WHERE`-clause index has no SQLAlchemy metadata equivalent, so
#: autogenerate always sees it as unmanaged.
#:
#: Each of these is covered by an integration test that proves the database actually
#: enforces it, so excluding them from the diff loses no real coverage - and keeps
#: `alembic check` meaningful for everything else.
UNMANAGED_CHECK_CONSTRAINTS = frozenset(
    {
        "ck_users_userrole",
        "ck_provider_slots_slotstatus",
        "ck_appointments_appointmentstatus",
        "ck_visits_visitstatus",
        "ck_waitlist_entries_waitliststatus",
    }
)

UNMANAGED_INDEXES = frozenset({"ux_appointments_live_slot"})


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Exclude objects autogenerate cannot compare, so real drift stays visible."""
    if type_ == "check_constraint" and name in UNMANAGED_CHECK_CONSTRAINTS:
        return False
    if type_ == "index" and name in UNMANAGED_INDEXES:
        return False
    return True


def _database_url() -> str:
    return Settings().postgres_dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
