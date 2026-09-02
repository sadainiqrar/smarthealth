"""Per-run PostgreSQL database lifecycle for the integration tier.

Each test run gets its own database on the shared stack, created and migrated at
session start and dropped at the end. This is what lets a shared compose stack behave
like a private one without ephemeral containers.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]


def quote_identifier(name: str) -> str:
    """Quote a SQL identifier.

    `CREATE DATABASE` cannot take a bound parameter, so the database name is
    interpolated — which means it must be escaped rather than trusted.
    """
    if "\x00" in name:
        raise ValueError("identifier contains a null byte")
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url)
    return config


async def _execute(admin_url: str, statement: str) -> None:
    connection = await asyncpg.connect(admin_url)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


async def create_database(admin_url: str, name: str) -> None:
    await _execute(admin_url, f"CREATE DATABASE {quote_identifier(name)}")


async def drop_database(admin_url: str, name: str) -> None:
    """Terminate stragglers first — Postgres refuses to drop a database in use."""
    await _execute(
        admin_url,
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{name}' AND pid <> pg_backend_pid()",
    )
    await _execute(admin_url, f"DROP DATABASE IF EXISTS {quote_identifier(name)}")


def create_database_sync(admin_url: str, name: str) -> None:
    asyncio.run(create_database(admin_url, name))


def drop_database_sync(admin_url: str, name: str) -> None:
    asyncio.run(drop_database(admin_url, name))


def run_migrations(settings_env: dict[str, str]) -> None:
    """Run `alembic upgrade head`.

    `migrations/env.py` builds its URL from `Settings()`, so the target database is
    selected through the environment rather than by threading a DSN through Alembic.
    """
    previous = {key: os.environ.get(key) for key in settings_env}
    os.environ.update(settings_env)
    try:
        command.upgrade(alembic_config(), "head")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
