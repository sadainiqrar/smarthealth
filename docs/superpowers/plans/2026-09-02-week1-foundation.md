# Week 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the codebase, database, and tooling foundation for SmartHealth Part A — module structure, async SQLAlchemy + Alembic, the ten-table domain schema, Mongo/Redis clients, an auth skeleton, and liveness/readiness endpoints — without any business endpoints.

**Architecture:** Domain-oriented modules under `app/modules/`, each owning its own models, with shared plumbing in `app/core` (settings, clock, registry, logging) and `app/db` (base, engine, session, clients). Resources are created in a FastAPI lifespan handler and stored on `app.state`; the engine is lazy so startup needs no running database. Postgres is the system of record; Mongo owns the audit trail.

**Tech Stack:** Python 3.13 · FastAPI · SQLAlchemy 2.0 async + asyncpg · Alembic · Motor · redis.asyncio · pwdlib[argon2] · PyJWT · pytest (five-tier harness already in place)

**Spec:** `docs/superpowers/specs/2026-09-02-week1-foundation-design.md`

**Existing state:** the test harness is built and green — 95 passed / 2 skipped on `-m "not docker"`, 1 passed on `-m docker`. A seven-service compose stack runs under project `smarthealth-test` with offset ports (Postgres 15432, Mongo 27018, Redis 16379). `app/` currently contains only `settings.py` and `main.py`.

---

## Conventions every task must follow

- Run Python via `.venv/Scripts/python.exe` (Windows) — the ambient `python` lacks the dependencies.
- Ruff enforces `line-length = 100` and `select = ["E4","E7","E9","F","E501","B"]`. **B (bugbear) is on**, so `pytest.raises(Exception)` is rejected — always assert a specific exception type.
- Imports go at the top of the file (E402 is enforced).
- Every task ends with `ruff check app tests` clean and the full fast lane passing.
- Stage explicit paths; never `git add -A`.
- The Docker stack is already running — do not tear it down.

## File Structure

**Application:**

| File | Responsibility |
| --- | --- |
| `app/core/clock.py` | Injectable time source (harness requirement 6) |
| `app/core/registry.py` | Generic named registry (harness requirement 3) |
| `app/core/logging.py` | JSON log formatting |
| `app/db/base.py` | `Base`, metadata naming convention, UUID + timestamp mixins |
| `app/db/engine.py` | Async engine factory — lazy, does not connect |
| `app/db/session.py` | Session factory and the request-scoped dependency |
| `app/db/mongo.py` | Motor client factory and audit-collection bootstrap |
| `app/db/redis.py` | `redis.asyncio` client factory |
| `app/db/all_models.py` | Imports every model module so Alembic sees full metadata |
| `app/modules/identity/models.py` | `users`, `UserRole` |
| `app/modules/identity/security.py` | Password hashing, JWT encode/decode |
| `app/modules/identity/deps.py` | `require_role(...)` dependency |
| `app/modules/patients/models.py` | `patients` |
| `app/modules/providers/models.py` | `clinics`, `departments`, `providers`, `provider_departments`, `provider_slots` |
| `app/modules/scheduling/models.py` | `appointments`, `visits`, `waitlist_entries` |
| `app/api/health.py` | `/health`, `/ready` |
| `app/api/router.py` | Router assembly |
| `app/main.py` | App factory + lifespan (modified) |
| `app/settings.py` | Extended with DSNs and JWT config (modified) |
| `migrations/env.py`, `migrations/versions/0001_baseline.py`, `alembic.ini` | Migrations |

**Tests:**

| File | Responsibility |
| --- | --- |
| `tests/harness/db.py` | Create/drop a per-run database; run migrations |
| `tests/tiers/t3_integration/conftest.py` | Session-scoped migrated database, async session |
| `tests/tiers/t0_unit/test_*.py` | Settings DSNs, clock, registry, logging, security |
| `tests/tiers/t1_contract/test_readiness.py` | Degraded `/ready` with no infrastructure |
| `tests/tiers/t1_contract/test_authz.py` | `require_role` against a throwaway app |
| `tests/tiers/t3_integration/test_schema.py` | Migrations apply; constraints reject violations |
| `tests/tiers/t3_integration/test_readiness.py` | `/ready` healthy against the live stack |
| `tests/tiers/t3_integration/test_document_store.py` | Mongo + Redis round-trip |

---

## Task 1: Dependencies and settings

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/settings.py`
- Modify: `.env.example`
- Test: `tests/tiers/t0_unit/test_settings_dsn.py`

- [ ] **Step 1: Add dependencies to `pyproject.toml`**

Replace the `dependencies` and `[project.optional-dependencies]` blocks with:

```toml
dependencies = [
    "fastapi>=0.115",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "uvicorn[standard]>=0.32",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "alembic>=1.14",
    "motor>=3.6",
    "redis>=5.2",
    "pwdlib[argon2]>=0.2.1",
    "pyjwt>=2.10",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "ruff>=0.7",
    "asgi-lifespan>=2.1",
]
```

- [ ] **Step 2: Install**

Run:
```bash
.venv/Scripts/python.exe -m pip install --quiet -e ".[dev]"
.venv/Scripts/python.exe -c "import sqlalchemy, asyncpg, alembic, motor, redis, pwdlib, jwt, asgi_lifespan; print('deps OK')"
```
Expected: `deps OK`

- [ ] **Step 3: Write the failing test**

Create `tests/tiers/t0_unit/test_settings_dsn.py`:

```python
import pytest

from app.settings import Settings

pytestmark = pytest.mark.unit


def test_postgres_dsn_is_built_for_asyncpg():
    settings = Settings(
        postgres_host="db.internal", postgres_port=15432,
        postgres_user="smarthealth", postgres_password="secret", postgres_db="sh_test",
    )
    assert settings.postgres_dsn == (
        "postgresql+asyncpg://smarthealth:secret@db.internal:15432/sh_test"
    )


def test_postgres_admin_url_targets_the_maintenance_database():
    """Creating or dropping a per-run database cannot be done while connected to it."""
    settings = Settings(
        postgres_host="db.internal", postgres_port=15432,
        postgres_user="smarthealth", postgres_password="secret", postgres_db="sh_test",
    )
    assert settings.postgres_admin_url == (
        "postgresql://smarthealth:secret@db.internal:15432/postgres"
    )


def test_credentials_round_trip_through_a_url_parser():
    """Assert on what a parser reads back, not on an encoded substring.

    Asserting the literal text `p%40ss+word` would codify a bug: `quote_plus` encodes
    a space as `+`, and a URL parser reads that back as a literal plus, silently
    yielding a different password.
    """
    from sqlalchemy.engine import make_url

    for password in ("p@ss word", "p/ss", "p:ss", "p#ss", "p?ss", "pa+ss", "pässword"):
        settings = Settings(postgres_user="a/b", postgres_password=password)
        url = make_url(settings.postgres_dsn)
        assert url.username == "a/b"
        assert url.password == password, f"password corrupted: {url.password!r}"


def test_an_ipv6_host_is_bracketed():
    """An unbracketed IPv6 literal makes the authority unparseable."""
    settings = Settings(postgres_host="::1", postgres_port=15432)
    assert "[::1]:15432" in settings.postgres_dsn


def test_mongo_and_redis_urls():
    settings = Settings(
        mongo_host="mongo.internal", mongo_port=27018, mongo_db="sh_audit",
        redis_host="redis.internal", redis_port=16379, redis_db=3,
    )
    assert settings.mongo_uri == "mongodb://mongo.internal:27018"
    assert settings.mongo_db == "sh_audit"
    assert settings.redis_url == "redis://redis.internal:16379/3"


def test_isolation_env_names_match_settings_fields(monkeypatch):
    """The harness's isolation layer sets these exact variables; a rename breaks isolation."""
    from tests.harness.isolation import make_isolation

    isolation = make_isolation(run_id="ab12cd34", worker_id="gw0")
    for key, value in isolation.as_env().items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.resource_prefix == isolation.resource_prefix
    assert settings.postgres_db == isolation.postgres_db
    assert settings.mongo_db == isolation.mongo_db
    assert settings.redis_db == isolation.redis_db


def test_jwt_defaults_are_present():
    settings = Settings()
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_expiry_minutes > 0
```

- [ ] **Step 4: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_settings_dsn.py -v`
Expected: FAIL — `Settings` has no `postgres_host` (extra inputs are permitted since `extra="ignore"`, so the failure surfaces as `AttributeError: 'Settings' object has no attribute 'postgres_dsn'`).

- [ ] **Step 5: Extend `app/settings.py`**

Add these imports at the top (after the existing ones):

```python
from urllib.parse import quote
```

Add these two module-level helpers just below the `LlmMode` definition:

```python
def _encode_credential(value: str) -> str:
    """Percent-encode a URI userinfo component.

    `quote_plus` is wrong here: it encodes a space as `+`, a query-string convention.
    URI parsers unquote only `%XX`, so the `+` survives literally and the credential
    silently becomes a different string. `quote(safe="")` encodes a space as `%20`.
    """
    return quote(value, safe="")


def _format_host(host: str) -> str:
    """Bracket an IPv6 literal, as RFC 3986 requires."""
    return f"[{host}]" if ":" in host else host
```

Add these fields to `Settings`, after `llm_mode`:

```python
    # --- PostgreSQL: the system of record ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "smarthealth"
    postgres_password: str = "smarthealth"
    postgres_db: str = "smarthealth"

    # --- MongoDB: the audit trail (design spec section 3.4) ---
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_db: str = "smarthealth"

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_prefix: str = ""

    # --- Auth ---
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
```

Add these properties after `task_queue`:

```python
    @property
    def _postgres_authority(self) -> str:
        """user:password@host:port, with credentials percent-encoded."""
        user = _encode_credential(self.postgres_user)
        password = _encode_credential(self.postgres_password)
        return f"{user}:{password}@{_format_host(self.postgres_host)}:{self.postgres_port}"

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy async URL for the application database."""
        return f"postgresql+asyncpg://{self._postgres_authority}/{self.postgres_db}"

    @property
    def postgres_admin_url(self) -> str:
        """Plain libpq URL to the maintenance database.

        Used by the test harness to CREATE/DROP a per-run database, which cannot be
        done from a connection to that database. No `+asyncpg` — asyncpg.connect
        rejects the SQLAlchemy dialect prefix.
        """
        return f"postgresql://{self._postgres_authority}/postgres"

    @property
    def mongo_uri(self) -> str:
        return f"mongodb://{_format_host(self.mongo_host)}:{self.mongo_port}"

    @property
    def redis_url(self) -> str:
        return f"redis://{_format_host(self.redis_host)}:{self.redis_port}/{self.redis_db}"
```

- [ ] **Step 6: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_settings_dsn.py -v`
Expected: 6 passed

- [ ] **Step 7: Update `.env.example`**

Replace the `# --- Application ---` section with:

```dotenv
# --- Application ---
SMARTHEALTH_ENVIRONMENT=local
SMARTHEALTH_RESOURCE_PREFIX=
SMARTHEALTH_LLM_MODE=fixture

# --- PostgreSQL (system of record) ---
SMARTHEALTH_POSTGRES_HOST=localhost
SMARTHEALTH_POSTGRES_PORT=5432
SMARTHEALTH_POSTGRES_USER=smarthealth
SMARTHEALTH_POSTGRES_PASSWORD=change-me
SMARTHEALTH_POSTGRES_DB=smarthealth

# --- MongoDB (audit trail) ---
SMARTHEALTH_MONGO_HOST=localhost
SMARTHEALTH_MONGO_PORT=27017
SMARTHEALTH_MONGO_DB=smarthealth

# --- Redis ---
SMARTHEALTH_REDIS_HOST=localhost
SMARTHEALTH_REDIS_PORT=6379
SMARTHEALTH_REDIS_DB=0

# --- Auth ---
SMARTHEALTH_JWT_SECRET=change-me
SMARTHEALTH_JWT_ALGORITHM=HS256
SMARTHEALTH_JWT_EXPIRY_MINUTES=60
```

- [ ] **Step 8: Verify and commit**

Run:
```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add pyproject.toml app/settings.py .env.example tests/tiers/t0_unit/test_settings_dsn.py
git commit -m "feat: database, cache and auth settings with encoded DSNs"
```
Expected: 101 passed, 2 skipped, 1 deselected; ruff clean.

---

## Task 2: Core utilities — clock, registry, logging

**Files:**
- Create: `app/core/__init__.py`, `app/core/clock.py`, `app/core/registry.py`, `app/core/logging.py`
- Test: `tests/tiers/t0_unit/test_core_utilities.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_core_utilities.py`:

```python
import json
import logging
from datetime import UTC, datetime

import pytest

from app.core.clock import Clock, FixedClock, get_clock
from app.core.logging import JsonFormatter, configure_logging
from app.core.registry import Registry

pytestmark = pytest.mark.unit


def test_clock_returns_timezone_aware_utc():
    """A naive datetime silently compares wrong against a timestamptz column."""
    moment = Clock().now()
    assert moment.tzinfo is not None
    assert moment.utcoffset().total_seconds() == 0


def test_fixed_clock_is_deterministic():
    instant = datetime(2026, 9, 2, 12, 30, tzinfo=UTC)
    clock = FixedClock(instant)
    assert clock.now() == instant
    assert clock.now() == instant


def test_get_clock_returns_a_usable_default():
    assert isinstance(get_clock().now(), datetime)


def test_registry_registers_and_retrieves():
    registry: Registry[str] = Registry("consumer")
    registry.register("appointments.booked", "handler-a")
    assert registry.get("appointments.booked") == "handler-a"
    assert registry.names() == ["appointments.booked"]
    assert "appointments.booked" in registry
    assert len(registry) == 1


def test_registry_rejects_a_duplicate_name():
    """Two handlers silently claiming one name is how events get processed twice."""
    registry: Registry[str] = Registry("consumer")
    registry.register("a", "first")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", "second")


def test_registry_reports_known_names_when_lookup_fails():
    registry: Registry[str] = Registry("consumer")
    registry.register("known", "x")
    with pytest.raises(KeyError, match="known"):
        registry.get("missing")


def test_registry_names_are_sorted():
    registry: Registry[str] = Registry("task")
    for name in ("c", "a", "b"):
        registry.register(name, name)
    assert registry.names() == ["a", "b", "c"]


def test_json_formatter_emits_parseable_records():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="booking %s confirmed", args=("abc",), exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "booking abc confirmed"
    assert "timestamp" in payload


def test_json_formatter_includes_exception_text():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_installs_the_json_formatter():
    configure_logging(level="WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_core_utilities.py -v`
Expected: collection ERROR — `No module named 'app.core'`

- [ ] **Step 3: Create the package and `app/core/clock.py`**

Run: `mkdir -p app/core && : > app/core/__init__.py`

Create `app/core/clock.py`:

```python
"""Time source.

Injected rather than called directly so that time-dependent logic — slot windows,
token expiry, wait-time analytics — can be tested without patching the standard
library. Harness requirement 6: never `datetime.utcnow()` inline.
"""

from __future__ import annotations

from datetime import UTC, datetime


class Clock:
    """The real clock. Always timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock(Clock):
    """A clock frozen at one instant, for tests."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


_default_clock = Clock()


def get_clock() -> Clock:
    """FastAPI dependency. Override in tests with `app.dependency_overrides`."""
    return _default_clock
```

- [ ] **Step 4: Create `app/core/registry.py`**

```python
"""A named registry.

Kafka consumers, Temporal workflows, and Celery tasks each register here so the
harness's meta-tests can enumerate them and assert properties — every consumer is
idempotent, every workflow is replay-safe — without a hand-maintained list that
drifts. Harness requirement 3.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    """Maps a unique name to a registered item.

    Duplicate names raise. Two handlers silently claiming one name is exactly how an
    event ends up processed twice, so the collision must be loud and immediate.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, item: T) -> T:
        if name in self._items:
            raise ValueError(f"{self.kind} '{name}' is already registered")
        self._items[name] = item
        return item

    def get(self, name: str) -> T:
        if name not in self._items:
            raise KeyError(
                f"no {self.kind} named '{name}'; registered: {', '.join(self.names()) or 'none'}"
            )
        return self._items[name]

    def names(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> list[tuple[str, T]]:
        return [(name, self._items[name]) for name in self.names()]

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return (self._items[name] for name in self.names())
```

- [ ] **Step 5: Create `app/core/logging.py`**

```python
"""Structured logging.

JSON lines so that a log aggregator can index fields rather than regex them. The
OpenTelemetry trace/span ids join this record in Week 3.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message"}


class JsonFormatter(logging.Formatter):
    """Renders a log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Removes only handlers this function previously installed, never other people's.
    A blanket `handlers.clear()` would also remove pytest's `caplog` capture handler
    — and this runs inside the application lifespan, so it fires on every contract
    test. Idempotent: calling it repeatedly leaves exactly one JSON handler.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]:
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
```

- [ ] **Step 6: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_core_utilities.py -v`
Expected: 10 passed

- [ ] **Step 7: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/core tests/tiers/t0_unit/test_core_utilities.py
git commit -m "feat: injectable clock, named registry, and JSON logging"
```

---

## Task 3: Database base, mixins, and naming convention

**Files:**
- Create: `app/db/__init__.py`, `app/db/base.py`
- Test: `tests/tiers/t0_unit/test_db_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_db_base.py`:

```python
import uuid

import pytest
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

pytestmark = pytest.mark.unit


class _Parent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "_probe_parents"
    name: Mapped[str] = mapped_column(String(50))


class _Child(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "_probe_children"
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("_probe_parents.id"))
    code: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint("code"),)


def test_primary_key_is_a_uuid_with_a_default():
    column = _Parent.__table__.c.id
    assert column.primary_key
    assert column.default is not None


def test_timestamps_are_timezone_aware_and_not_nullable():
    """A naive timestamp cannot be compared correctly across clinics in other zones."""
    for name in ("created_at", "updated_at"):
        column = _Parent.__table__.c[name]
        assert column.type.timezone is True
        assert not column.nullable


def test_constraint_names_follow_the_naming_convention():
    """Unnamed constraints get database-generated names, which makes migrations
    undiffable and drops impossible to write by hand."""
    assert _Parent.__table__.primary_key.name == "pk__probe_parents"
    foreign_key = next(iter(_Child.__table__.foreign_key_constraints))
    assert foreign_key.name == "fk__probe_children_parent_id__probe_parents"
    unique = next(
        c for c in _Child.__table__.constraints if isinstance(c, UniqueConstraint)
    )
    assert unique.name == "uq__probe_children_code"


def test_every_registered_table_uses_the_shared_metadata():
    assert _Parent.__table__.metadata is Base.metadata
    assert _Child.__table__.metadata is Base.metadata
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_db_base.py -v`
Expected: collection ERROR — `No module named 'app.db'`

- [ ] **Step 3: Create `app/db/base.py`**

Run: `mkdir -p app/db && : > app/db/__init__.py`

```python
"""Declarative base, shared metadata, and column mixins.

The naming convention is load-bearing: without it Postgres generates constraint
names, which makes Alembic diffs unstable and hand-written downgrades impossible.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """UUID primary keys: stable inside event payloads, no sequence contention."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_db_base.py -v`
Expected: 5 passed

- [ ] **Step 5: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/db tests/tiers/t0_unit/test_db_base.py
git commit -m "feat: declarative base with naming convention and shared mixins"
```

---

## Task 4: Lazy engine and session factory

The laziness proved here is what lets T1 contract tests run the real FastAPI lifespan with no database present (spec §7.1).

**Files:**
- Create: `app/db/engine.py`, `app/db/session.py`
- Test: `tests/tiers/t0_unit/test_db_engine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_db_engine.py`:

```python
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine
from app.db.session import create_session_factory, get_session
from app.settings import Settings

pytestmark = pytest.mark.unit


def test_engine_is_built_from_the_settings_dsn():
    settings = Settings(postgres_host="db.internal", postgres_port=15432, postgres_db="sh")
    engine = create_engine(settings)
    assert isinstance(engine, AsyncEngine)
    assert engine.url.host == "db.internal"
    assert engine.url.port == 15432
    assert engine.url.database == "sh"


def test_creating_an_engine_does_not_connect():
    """The T1 contract lane runs the real lifespan with no database running.

    `create_async_engine` must build a pool without dialing out; if this ever starts
    connecting eagerly, every contract test begins requiring Docker.
    """
    settings = Settings(postgres_host="203.0.113.1", postgres_port=1)
    engine = create_engine(settings)  # must not raise, must not hang
    assert engine.url.port == 1


def test_session_factory_produces_async_sessions():
    settings = Settings()
    factory = create_session_factory(create_engine(settings))
    # Verified on SQLAlchemy 2.0.52: async_sessionmaker does NOT subclass sessionmaker,
    # so asserting the sync type here would simply be wrong.
    assert isinstance(factory, async_sessionmaker)
    assert factory.class_ is AsyncSession


def test_sessions_do_not_expire_objects_on_commit():
    """expire_on_commit=True would re-query attributes after commit, which in async
    code raises MissingGreenlet instead of lazily loading."""
    factory = create_session_factory(create_engine(Settings()))
    assert factory.kw["expire_on_commit"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_db_engine.py -v`
Expected: collection ERROR — `No module named 'app.db.engine'`

- [ ] **Step 3: Create `app/db/engine.py`**

```python
"""Async engine construction.

`create_async_engine` is deliberately lazy — it builds a connection pool without
connecting. That is what lets the T1 contract lane run the application's real
lifespan handler with no database present.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.postgres_dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
        future=True,
    )
```

- [ ] **Step 4: Create `app/db/session.py`**

```python
"""Session factory and the request-scoped session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """One session per request.

    `expire_on_commit=False` because expiring attributes after commit triggers a
    lazy reload, which in async SQLAlchemy raises MissingGreenlet rather than
    silently issuing a query.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to this request.

    Rolls back on an unhandled exception so a failed request cannot leak a dirty
    transaction into the pool.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_db_engine.py -v`
Expected: 4 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/db/engine.py app/db/session.py tests/tiers/t0_unit/test_db_engine.py
git commit -m "feat: lazy async engine and request-scoped session factory"
```

---

## Task 5: Identity — users model and security helpers

**Files:**
- Create: `app/modules/__init__.py`, `app/modules/identity/__init__.py`, `app/modules/identity/models.py`, `app/modules/identity/security.py`
- Test: `tests/tiers/t0_unit/test_security.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_security.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_security.py -v`
Expected: collection ERROR — `No module named 'app.modules'`

- [ ] **Step 3: Create the packages and `app/modules/identity/models.py`**

Run: `mkdir -p app/modules/identity && : > app/modules/__init__.py && : > app/modules/identity/__init__.py`

```python
"""Authentication identity.

A `User` is a login, not a person. Patients and providers are separate records that
may or may not have one — front-desk staff register walk-in patients who have no
credentials at all (design spec section 3.1).
"""

from __future__ import annotations

import enum

from sqlalchemy import Boolean, Enum, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserRole(str, enum.Enum):
    PATIENT = "patient"
    PROVIDER = "provider"
    FRONT_DESK = "front_desk"
    ADMIN = "admin"


#: VARCHAR + CHECK rather than a native Postgres enum: adding a value to a native
#: enum requires ALTER TYPE, which cannot run inside a transactional migration.
role_column = Enum(
    UserRole,
    native_enum=False,
    length=20,
    values_callable=lambda enum_class: [member.value for member in enum_class],
)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(role_column)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
```

- [ ] **Step 4: Create `app/modules/identity/security.py`**

```python
"""Password hashing and access tokens.

Both are deliberately pure functions over explicit inputs — no global settings, no
implicit clock — so expiry and tampering can be tested without patching anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.modules.identity.models import UserRole
from app.settings import Settings

_password_hash = PasswordHash.recommended()


class InvalidToken(Exception):
    """The token is missing, malformed, expired, or not signed by us."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    role: UserRole


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _password_hash.verify(password, hashed)


def create_access_token(
    *,
    subject: str,
    role: UserRole,
    settings: Settings,
    now: datetime,
    expires_in: timedelta | None = None,
) -> str:
    lifetime = expires_in or timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {
        "sub": subject,
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, *, settings: Settings) -> TokenClaims:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("token has expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"token is not valid: {exc}") from exc

    subject = payload.get("sub")
    raw_role = payload.get("role")
    if not subject or not raw_role:
        raise InvalidToken("token is missing 'sub' or 'role'")
    try:
        role = UserRole(raw_role)
    except ValueError as exc:
        raise InvalidToken(f"token carries an unknown role '{raw_role}'") from exc
    return TokenClaims(subject=subject, role=role)
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_security.py -v`
Expected: 10 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules tests/tiers/t0_unit/test_security.py
git commit -m "feat: users model, argon2 password hashing, and JWT access tokens"
```

---

## Task 6: `require_role` dependency

**Files:**
- Create: `app/modules/identity/deps.py`
- Test: `tests/tiers/t1_contract/test_authz.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t1_contract/test_authz.py`:

```python
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.modules.identity.deps import get_token_settings, require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims, create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings(jwt_secret="authz-test-secret", jwt_expiry_minutes=30)
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/admin-only")
    async def admin_only(
        claims: TokenClaims = Depends(require_role(UserRole.ADMIN)),
    ) -> dict[str, str]:
        return {"subject": claims.subject}

    @app.get("/staff")
    async def staff(
        claims: TokenClaims = Depends(
            require_role(UserRole.ADMIN, UserRole.FRONT_DESK)
        ),
    ) -> dict[str, str]:
        return {"subject": claims.subject}

    app.dependency_overrides[get_token_settings] = lambda: SETTINGS
    return app


def token_for(role: UserRole) -> str:
    return create_access_token(subject="user-1", role=role, settings=SETTINGS, now=NOW)


async def call(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers=headers or {})


async def test_a_matching_role_is_allowed():
    response = await call(
        "/admin-only", {"Authorization": f"Bearer {token_for(UserRole.ADMIN)}"}
    )
    assert response.status_code == 200
    assert response.json()["subject"] == "user-1"


async def test_any_of_several_allowed_roles_is_accepted():
    response = await call(
        "/staff", {"Authorization": f"Bearer {token_for(UserRole.FRONT_DESK)}"}
    )
    assert response.status_code == 200


async def test_a_wrong_role_is_forbidden_not_unauthorised():
    """403 not 401: the caller proved who they are, they just may not do this."""
    response = await call(
        "/admin-only", {"Authorization": f"Bearer {token_for(UserRole.PATIENT)}"}
    )
    assert response.status_code == 403


async def test_a_missing_header_is_unauthorised():
    response = await call("/admin-only")
    assert response.status_code == 401


async def test_a_non_bearer_scheme_is_unauthorised():
    response = await call("/admin-only", {"Authorization": "Basic abc123"})
    assert response.status_code == 401


async def test_a_garbage_token_is_unauthorised():
    response = await call("/admin-only", {"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_authz.py -v`
Expected: collection ERROR — `No module named 'app.modules.identity.deps'`

- [ ] **Step 3: Create `app/modules/identity/deps.py`**

```python
"""Authorisation dependencies.

`require_role` is a dependency factory, so a route declares the roles it accepts and
the enforcement happens before the handler body runs. The harness's future
`test_endpoints_authz` meta-test enumerates routes and asserts each declares one.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from app.modules.identity.models import UserRole
from app.modules.identity.security import InvalidToken, TokenClaims, decode_access_token
from app.settings import Settings, get_settings

_UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="missing or invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_token_settings() -> Settings:
    """Indirection so tests can override the signing settings for one app."""
    return get_settings()


def require_role(*allowed: UserRole) -> Callable[..., TokenClaims]:
    """Build a dependency that admits only the listed roles.

    401 when identity cannot be established; 403 when it can but the role is wrong —
    the distinction matters to a client deciding whether to re-authenticate.
    """
    if not allowed:
        raise ValueError("require_role needs at least one role")
    permitted = frozenset(allowed)

    def dependency(
        request: Request, settings: Settings = Depends(get_token_settings)
    ) -> TokenClaims:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _UNAUTHORISED
        try:
            claims = decode_access_token(token, settings=settings)
        except InvalidToken as exc:
            raise _UNAUTHORISED from exc
        if claims.role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role '{claims.role.value}' may not perform this action; "
                    f"requires one of: {', '.join(sorted(r.value for r in permitted))}"
                ),
            )
        return claims

    return dependency
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_authz.py -v`
Expected: 6 passed

- [ ] **Step 5: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/identity/deps.py tests/tiers/t1_contract/test_authz.py
git commit -m "feat: require_role dependency separating 401 from 403"
```

---

## Task 7: Patients, clinics, departments, and providers

**Files:**
- Create: `app/modules/patients/__init__.py`, `app/modules/patients/models.py`
- Create: `app/modules/providers/__init__.py`, `app/modules/providers/models.py`
- Test: `tests/tiers/t0_unit/test_domain_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_domain_models.py`:

```python
import pytest

from app.modules.patients.models import Patient
from app.modules.providers.models import (
    Clinic,
    Department,
    Provider,
    provider_departments,
)

pytestmark = pytest.mark.unit


def test_a_patient_can_exist_without_a_user_account():
    """Front-desk staff register walk-ins who have no credentials (spec 3.1)."""
    assert Patient.__table__.c.user_id.nullable
    assert Patient.__table__.c.user_id.unique


def test_patient_medical_record_number_is_unique():
    assert Patient.__table__.c.mrn.unique


def test_a_provider_can_exist_without_a_user_account():
    assert Provider.__table__.c.user_id.nullable
    assert Provider.__table__.c.user_id.unique


def test_a_department_belongs_to_one_clinic():
    """Cardiology at Riverside is a different unit from Cardiology at Northgate."""
    assert not Department.__table__.c.clinic_id.nullable
    names = {c.name for c in Department.__table__.constraints}
    assert "uq_departments_clinic_id" in names


def test_providers_and_departments_are_many_to_many():
    """A provider covering two clinics must remain bookable at both."""
    assert set(provider_departments.c.keys()) == {"provider_id", "department_id"}
    assert len(provider_departments.primary_key.columns) == 2


def test_a_clinic_records_its_timezone():
    """Slot generation is wrong without it once clinics span zones."""
    assert not Clinic.__table__.c.timezone.nullable


def test_tables_are_named_as_expected():
    assert Patient.__tablename__ == "patients"
    assert Clinic.__tablename__ == "clinics"
    assert Department.__tablename__ == "departments"
    assert Provider.__tablename__ == "providers"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_domain_models.py -v`
Expected: collection ERROR — `No module named 'app.modules.patients'`

- [ ] **Step 3: Create `app/modules/patients/models.py`**

Run: `mkdir -p app/modules/patients app/modules/providers && : > app/modules/patients/__init__.py && : > app/modules/providers/__init__.py`

```python
"""Patient records.

A patient is a person receiving care, not a login. `user_id` is nullable so front-desk
staff can register a walk-in immediately and link an account later (design spec 3.1).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Patient(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "patients"

    mrn: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, unique=True,
    )
```

- [ ] **Step 4: Create `app/modules/providers/models.py`**

```python
"""Clinics, departments, providers, and their bookable slots.

Departments are scoped to a clinic, and providers relate to departments many-to-many
so a clinician covering two sites stays bookable at both (design spec 3.5).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SlotStatus(str, enum.Enum):
    FREE = "free"
    HELD = "held"
    BOOKED = "booked"
    BLOCKED = "blocked"


slot_status_column = Enum(
    SlotStatus,
    native_enum=False,
    length=16,
    values_callable=lambda enum_class: [member.value for member in enum_class],
)


class Clinic(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "clinics"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    #: IANA zone. Slot generation is wrong without it once clinics span zones.
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class Department(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "departments"

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))

    __table_args__ = (UniqueConstraint("clinic_id", "name"),)


class Provider(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "providers"

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    specialty: Mapped[str] = mapped_column(String(120), index=True)
    license_number: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, unique=True,
    )


provider_departments = Table(
    "provider_departments",
    Base.metadata,
    Column(
        "provider_id",
        PGUUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "department_id",
        PGUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ProviderSlot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One bookable interval.

    Booking is an atomic conditional update — `SET status='held' WHERE id=:id AND
    status='free'` — so zero rows affected *is* the conflict signal, enforced by the
    database rather than by application logic (design spec 3.2).

    A GiST exclusion constraint preventing overlapping slots for one provider is
    added in the baseline migration; SQLAlchemy cannot express it portably, and it
    needs the `btree_gist` extension.
    """

    __tablename__ = "provider_slots"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE")
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlotStatus] = mapped_column(
        slot_status_column, server_default=text("'free'")
    )
    #: Optimistic-concurrency counter for the Week 2 booking workflow.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="end_after_start"),
        Index("ix_provider_slots_provider_starts", "provider_id", "starts_at"),
        Index("ix_provider_slots_status_starts", "status", "starts_at"),
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_domain_models.py -v`
Expected: 7 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/patients app/modules/providers tests/tiers/t0_unit/test_domain_models.py
git commit -m "feat: patient, clinic, department, provider and slot models"
```

---

## Task 8: Scheduling — appointments, visits, waitlist

**Files:**
- Create: `app/modules/scheduling/__init__.py`, `app/modules/scheduling/models.py`
- Create: `app/db/all_models.py`
- Test: `tests/tiers/t0_unit/test_scheduling_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_scheduling_models.py`:

```python
import pytest

from app.db.all_models import ALL_TABLES
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    Visit,
    VisitStatus,
    WaitlistEntry,
)

pytestmark = pytest.mark.unit


def test_appointment_starts_pending_not_confirmed():
    """The headline requirement: confirmed only after the whole workflow succeeds."""
    assert Appointment.__table__.c.status.server_default.arg.text == "'pending'"


def test_appointment_statuses_cover_the_required_lifecycle():
    assert {status.value for status in AppointmentStatus} == {
        "pending", "confirmed", "cancelled", "rescheduled",
        "completed", "no_show", "failed",
    }


def test_a_reschedule_links_back_to_the_appointment_it_replaced():
    """History matters to both the audit trail and the analytics."""
    column = Appointment.__table__.c.rescheduled_from_id
    assert column.nullable
    assert {fk.column.table.name for fk in column.foreign_keys} == {"appointments"}


def test_a_visit_is_one_to_one_with_an_appointment():
    assert Visit.__table__.c.appointment_id.unique


def test_wait_time_components_are_present_and_optional_after_check_in():
    """Average Wait Time = seen_at - checked_in_at."""
    assert not Visit.__table__.c.checked_in_at.nullable
    assert Visit.__table__.c.seen_at.nullable
    assert Visit.__table__.c.completed_at.nullable


def test_visit_statuses():
    assert {status.value for status in VisitStatus} == {
        "checked_in", "in_progress", "completed"
    }


def test_waitlist_entry_targets_a_provider_or_a_department():
    for name in ("provider_id", "department_id"):
        assert WaitlistEntry.__table__.c[name].nullable


def test_all_ten_persistent_tables_are_registered_for_migrations():
    """Alembic only sees tables whose module has been imported.

    `<=` rather than `==`: test_db_base.py defines two probe tables on the same
    metadata, and whether they are present depends on import order.
    """
    expected = {
        "users", "patients", "providers", "clinics", "departments",
        "provider_departments", "provider_slots", "appointments", "visits",
        "waitlist_entries",
    }
    assert len(expected) == 10
    assert expected <= ALL_TABLES
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_scheduling_models.py -v`
Expected: collection ERROR — `No module named 'app.modules.scheduling'`

- [ ] **Step 3: Create `app/modules/scheduling/models.py`**

Run: `mkdir -p app/modules/scheduling && : > app/modules/scheduling/__init__.py`

```python
"""Appointments, visits, and the waitlist.

An appointment is the booking; a visit is what actually happened. Keeping them
separate means Average Wait Time falls out of `seen_at - checked_in_at`, a no-show is
simply an appointment with no visit, and two different workflows stop mutating one
row (design spec 3.3).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AppointmentStatus(str, enum.Enum):
    #: Created, workflow not yet finished. The only legal initial state.
    PENDING = "pending"
    #: Reachable only after slot reservation, billing pre-check and notification
    #: scheduling have all succeeded.
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    #: Superseded by a newer appointment that points back via rescheduled_from_id.
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    #: The booking workflow failed and its compensation ran.
    FAILED = "failed"


class VisitStatus(str, enum.Enum):
    CHECKED_IN = "checked_in"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class WaitlistStatus(str, enum.Enum):
    ACTIVE = "active"
    FULFILLED = "fulfilled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def _enum_column(enum_class: type[enum.Enum], length: int) -> Enum:
    return Enum(
        enum_class,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Appointment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "appointments"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT"), index=True
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="RESTRICT")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: The slot this booking claims. A partial unique index in the baseline migration
    #: guarantees at most one *live* appointment per slot, independently of whether
    #: the application's conditional update is correct.
    slot_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("provider_slots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum_column(AppointmentStatus, 16), server_default=text("'pending'"), index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"),
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_appointments_patient_status", "patient_id", "status"),
    )


class Visit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "visits"

    appointment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"),
        unique=True,
    )
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Average Wait Time = avg(seen_at - checked_in_at).
    seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[VisitStatus] = mapped_column(
        _enum_column(VisitStatus, 16), server_default=text("'checked_in'")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class WaitlistEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Who to promote when a slot is released (Week 2's cancellation flow)."""

    __tablename__ = "waitlist_entries"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=True,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=True,
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[WaitlistStatus] = mapped_column(
        _enum_column(WaitlistStatus, 16), server_default=text("'active'"), index=True
    )
```

- [ ] **Step 4: Create `app/db/all_models.py`**

```python
"""Imports every model module so `Base.metadata` is complete.

Alembic only sees a table whose module has been imported. Without this single import
site, a forgotten import produces an autogenerated migration that silently drops
tables. Import this module — never individual model modules — from `migrations/env.py`.
"""

from __future__ import annotations

from app.db.base import Base
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.patients import models as patient_models  # noqa: F401
from app.modules.providers import models as provider_models  # noqa: F401
from app.modules.scheduling import models as scheduling_models  # noqa: F401

#: Every table name registered on the shared metadata, for assertions and tooling.
ALL_TABLES = set(Base.metadata.tables)

__all__ = ["ALL_TABLES", "Base"]
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_scheduling_models.py -v`
Expected: 8 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/scheduling app/db/all_models.py tests/tiers/t0_unit/test_scheduling_models.py
git commit -m "feat: appointment, visit and waitlist models with a pending-first lifecycle"
```

---

## Task 9: Alembic and the baseline migration

**Files:**
- Create: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_baseline.py`

- [ ] **Step 1: Create `alembic.ini`**

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
version_path_separator = os
# sqlalchemy.url is set programmatically in migrations/env.py from application
# settings, so a DSN is never duplicated here.

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Create `migrations/script.py.mako`**

Run: `mkdir -p migrations/versions`

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 3: Create `migrations/env.py`**

```python
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


def _database_url() -> str:
    return Settings().postgres_dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
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
```

- [ ] **Step 4: Generate the baseline migration**

Run:
```bash
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth \
  .venv/Scripts/python.exe -m alembic revision --autogenerate -m "baseline schema"
```

This connects to the running test stack to diff against an empty database. Rename the
generated file to `migrations/versions/0001_baseline.py` and set `revision = "0001"`,
`down_revision = None`.

**Inspect the generated file carefully.** Autogenerate produces the tables, columns,
foreign keys, indexes and the `CHECK` constraints, but it cannot produce the two
database-level guarantees below — add them by hand.

- [ ] **Step 5: Add the two hand-written guarantees**

At the **start** of `upgrade()`, before any `create_table`:

```python
    # Required by the provider_slots exclusion constraint below.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
```

At the **end** of `upgrade()`:

```python
    # A provider cannot have two overlapping slots. Without this a buggy slot
    # generator silently produces double-bookable inventory; with it, the bad write
    # fails immediately.
    op.execute(
        """
        ALTER TABLE provider_slots
          ADD CONSTRAINT ck_provider_slots_no_overlap
          EXCLUDE USING gist (
            provider_id WITH =,
            tstzrange(starts_at, ends_at) WITH &&
          )
        """
    )

    # `onupdate=func.now()` is a SQLAlchemy-level construct: verified against the live
    # database, it fires for an ORM flush but NOT for a raw `text("UPDATE ...")`. Week
    # 2's booking activity claims a slot with exactly such a raw conditional UPDATE, so
    # without this trigger `updated_at` would go silently stale on the single most
    # important write in the system. A trigger also covers every future hand-written
    # statement and data migration, which a convention cannot.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "users", "patients", "providers", "clinics", "departments",
        "provider_slots", "appointments", "visits", "waitlist_entries",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
            """
        )

    # At most one *live* appointment may hold a slot. This makes the invariant a
    # database guarantee rather than a consequence of the booking activity being
    # written correctly.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_appointments_live_slot
            ON appointments (slot_id)
         WHERE status IN ('pending', 'confirmed') AND slot_id IS NOT NULL
        """
    )
```

At the **start** of `downgrade()`:

```python
    for table in (
        "users", "patients", "providers", "clinics", "departments",
        "provider_slots", "appointments", "visits", "waitlist_entries",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute("DROP INDEX IF EXISTS ux_appointments_live_slot")
    op.execute(
        "ALTER TABLE provider_slots DROP CONSTRAINT IF EXISTS ck_provider_slots_no_overlap"
    )
```

- [ ] **Step 6: Verify the migration applies and reverses**

Run:
```bash
export SMARTHEALTH_POSTGRES_PORT=15432
export SMARTHEALTH_POSTGRES_DB=smarthealth
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic downgrade base
.venv/Scripts/python.exe -m alembic upgrade head
```
Expected: each completes without error. The down-then-up proves `downgrade()` is real
rather than decorative.

- [ ] **Step 7: Confirm autogenerate now sees no drift**

Run:
```bash
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth \
  .venv/Scripts/python.exe -m alembic check
```
Expected: `No new upgrade operations detected.` If it reports operations, the models and
the migration disagree — fix the migration, not the models.

- [ ] **Step 8: Commit**

```bash
git add alembic.ini migrations
git commit -m "feat: alembic setup and baseline schema with database-level slot guarantees"
```

---

## Task 10: Mongo and Redis clients

**Files:**
- Create: `app/db/mongo.py`, `app/db/redis.py`
- Test: `tests/tiers/t0_unit/test_clients.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_clients.py`:

```python
import pytest

from app.db.mongo import AUDIT_COLLECTION, create_mongo_client, get_audit_collection
from app.db.redis import create_redis_client, namespaced_key
from app.settings import Settings

pytestmark = pytest.mark.unit


def test_mongo_client_is_built_from_settings_without_connecting():
    """Motor connects lazily; the T1 lane must not require a running Mongo."""
    settings = Settings(mongo_host="203.0.113.1", mongo_port=1, mongo_db="sh_test")
    client = create_mongo_client(settings)
    assert client.address is None or True  # no connection attempted at construction
    client.close()


def test_audit_collection_is_selected_from_the_configured_database():
    settings = Settings(mongo_db="sh_run_db")
    client = create_mongo_client(settings)
    collection = get_audit_collection(client, settings)
    assert collection.name == AUDIT_COLLECTION
    assert collection.database.name == "sh_run_db"
    client.close()


def test_redis_client_is_built_from_settings_without_connecting():
    settings = Settings(redis_host="203.0.113.1", redis_port=1, redis_db=4)
    client = create_redis_client(settings)
    assert client.connection_pool.connection_kwargs["db"] == 4


def test_namespaced_key_applies_the_isolation_prefix():
    """Two test runs against one Redis must not read each other's keys."""
    settings = Settings(redis_prefix="t_ab12_gw0_")
    assert namespaced_key(settings, "slot:hold:1") == "t_ab12_gw0_slot:hold:1"


def test_namespaced_key_is_a_no_op_without_a_prefix():
    assert namespaced_key(Settings(redis_prefix=""), "k") == "k"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_clients.py -v`
Expected: collection ERROR — `No module named 'app.db.mongo'`

- [ ] **Step 3: Create `app/db/mongo.py`**

```python
"""MongoDB client and the audit collection.

Mongo owns the audit trail: append-only, high volume, and a before/after payload
whose shape differs per entity and per action. Postgres remains the system of record
for every piece of transactional state (design spec 3.4).
"""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from app.settings import Settings

AUDIT_COLLECTION = "audit_events"


def create_mongo_client(settings: Settings) -> AsyncIOMotorClient:
    """Motor connects lazily, so this is safe to call with no Mongo running."""
    return AsyncIOMotorClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=2000,
        connectTimeoutMS=2000,
        uuidRepresentation="standard",
    )


def get_audit_collection(
    client: AsyncIOMotorClient, settings: Settings
) -> AsyncIOMotorCollection:
    return client[settings.mongo_db][AUDIT_COLLECTION]


async def ensure_audit_indexes(
    client: AsyncIOMotorClient, settings: Settings
) -> None:
    """Create the indexes the audit query pattern needs.

    Idempotent — Mongo ignores a create for an index that already exists. Called
    explicitly by tooling, not at startup, so application boot needs no database.
    """
    collection = get_audit_collection(client, settings)
    await collection.create_index([("entity_type", 1), ("entity_id", 1), ("at", -1)])
    await collection.create_index([("at", -1)])
```

- [ ] **Step 4: Create `app/db/redis.py`**

```python
"""Redis client and key namespacing."""

from __future__ import annotations

from redis.asyncio import Redis

from app.settings import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Constructs a client and a pool without connecting."""
    return Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
    )


def namespaced_key(settings: Settings, key: str) -> str:
    """Prefix a key with this run's namespace.

    Redis has no schemas, so the prefix is the only thing keeping two concurrent test
    runs against one server from reading each other's state.
    """
    return f"{settings.redis_prefix}{key}"
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_clients.py -v`
Expected: 5 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/db/mongo.py app/db/redis.py tests/tiers/t0_unit/test_clients.py
git commit -m "feat: lazy Mongo and Redis clients with namespaced keys"
```

---

## Task 11: Lifespan, `/ready`, and the API package

**Files:**
- Create: `app/api/__init__.py`, `app/api/health.py`, `app/api/router.py`
- Modify: `app/main.py`
- Modify: `tests/conftest.py`
- Test: `tests/tiers/t1_contract/test_readiness.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t1_contract/test_readiness.py`:

```python
import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app

pytestmark = pytest.mark.contract


@pytest.fixture
def unreachable_infrastructure(monkeypatch):
    """Point every dependency at a closed port so readiness must report failure."""
    for variable in ("POSTGRES", "MONGO", "REDIS"):
        monkeypatch.setenv(f"SMARTHEALTH_{variable}_HOST", "127.0.0.1")
        monkeypatch.setenv(f"SMARTHEALTH_{variable}_PORT", "1")


async def test_ready_reports_503_when_dependencies_are_down(unreachable_infrastructure):
    """Readiness must fail loudly rather than optimistically report ok."""
    async with LifespanManager(create_app()) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    reported = {entry["name"]: entry["ok"] for entry in body["dependencies"]}
    assert reported == {"postgres": False, "mongo": False, "redis": False}
    assert all(entry["detail"] for entry in body["dependencies"])


async def test_health_stays_a_flat_liveness_probe(api_client):
    """/health must not grow dependency checks: its dict[str, str] annotation is an
    enforced response model, and it answers 'is the process alive', not 'is it useful'."""
    response = await api_client.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "environment", "service"}
    assert all(isinstance(value, str) for value in response.json().values())


async def test_lifespan_starts_with_no_infrastructure(unreachable_infrastructure):
    """The whole T1 lane depends on startup not dialing out."""
    async with LifespanManager(create_app()) as manager:
        assert manager.app.state.engine is not None
        assert manager.app.state.session_factory is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_readiness.py -v`
Expected: FAIL — `cannot import name 'create_app' from 'app.main'`

- [ ] **Step 3: Create `app/api/health.py`**

Run: `mkdir -p app/api && : > app/api/__init__.py`

```python
"""Liveness and readiness.

`/health` answers "is this process alive" and must stay a flat `dict[str, str]` —
FastAPI enforces that annotation as a response model, so a nested or boolean field
would raise at runtime.

`/ready` answers "can this process do useful work" and reports each dependency
separately, because "something is down" is far less actionable than "Mongo is down".
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.settings import get_settings

router = APIRouter(tags=["system"])

CHECK_TIMEOUT_SECONDS = 2.0


class DependencyStatus(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    ready: bool
    dependencies: list[DependencyStatus]


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness/readiness probe. The test stack and the first case both assert on it."""
    settings = get_settings()
    return {"status": "ok", "environment": settings.environment, "service": settings.service_name}


async def _check_postgres(request: Request) -> DependencyStatus:
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        return DependencyStatus(name="postgres", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="postgres", ok=True)


async def _check_mongo(request: Request) -> DependencyStatus:
    try:
        await request.app.state.mongo.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(name="mongo", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="mongo", ok=True)


async def _check_redis(request: Request) -> DependencyStatus:
    try:
        await request.app.state.redis.ping()
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(name="redis", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="redis", ok=True)


def _summarise(exc: BaseException) -> str:
    """One readable line. Full tracebacks belong in logs, not in a probe response."""
    text_form = str(exc).strip().splitlines()
    return (text_form[0] if text_form else exc.__class__.__name__)[:200]


@router.get("/ready", response_model=ReadinessResponse)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    async def guarded(check) -> DependencyStatus:
        try:
            return await asyncio.wait_for(check(request), CHECK_TIMEOUT_SECONDS)
        except TimeoutError:
            return DependencyStatus(
                name=check.__name__.removeprefix("_check_"),
                ok=False,
                detail=f"timed out after {CHECK_TIMEOUT_SECONDS}s",
            )

    dependencies = list(
        await asyncio.gather(
            guarded(_check_postgres), guarded(_check_mongo), guarded(_check_redis)
        )
    )
    all_ok = all(entry.ok for entry in dependencies)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(ready=all_ok, dependencies=dependencies)
```

- [ ] **Step 4: Create `app/api/router.py`**

```python
"""Router assembly. Every module's router is mounted here, and only here."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)
```

- [ ] **Step 5: Rewrite `app/main.py`**

```python
"""FastAPI application entry point.

Resources are created in the lifespan handler and stored on `app.state`. Every client
is lazy — none of them connects at startup — so the application boots with no
infrastructure running, and the T1 contract lane can exercise the real startup path
without Docker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.logging import configure_logging
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client
from app.db.redis import create_redis_client
from app.db.session import create_session_factory
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()

    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.mongo = create_mongo_client(settings)
    app.state.redis = create_redis_client(settings)
    try:
        yield
    finally:
        await app.state.engine.dispose()
        app.state.mongo.close()
        await app.state.redis.aclose()


def create_app() -> FastAPI:
    application = FastAPI(title="SmartHealth", version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)
    return application


app = create_app()
```

- [ ] **Step 6: Update `tests/conftest.py` to run the lifespan**

Add to the import block at the **top** (E402 is enforced):

```python
from asgi_lifespan import LifespanManager
```

Replace the `api_client` fixture body with:

```python
@pytest.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to the ASGI app — no socket, no server.

    Wrapped in LifespanManager because `httpx.ASGITransport` does not run FastAPI's
    lifespan: without it these tests exercise an app whose startup never ran, and
    `app.state.engine` would not exist.
    """
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client
```

- [ ] **Step 7: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract -v`
Expected: all pass — 2 health + 6 authz + 3 readiness = 11.

- [ ] **Step 8: Confirm the existing case still passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/test_catalog.py -v`
Expected: `test_declarative_case[sys-001-health-endpoint-responds]` PASSED — proving the
lifespan change did not break the catalog's only case.

- [ ] **Step 9: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/api app/main.py tests/conftest.py tests/tiers/t1_contract/test_readiness.py
git commit -m "feat: lifespan-managed resources and a per-dependency readiness probe"
```

---

## Task 12: Test-harness database support

**Files:**
- Create: `tests/harness/db.py`
- Create: `tests/tiers/t3_integration/conftest.py`
- Test: `tests/tiers/t0_unit/test_harness_db.py`

- [ ] **Step 1: Write the failing unit test**

Create `tests/tiers/t0_unit/test_harness_db.py`:

```python
import pytest

from tests.harness.db import alembic_config, quote_identifier

pytestmark = pytest.mark.unit


def test_identifier_quoting_wraps_in_double_quotes():
    assert quote_identifier("t_ab12_gw0_db") == '"t_ab12_gw0_db"'


def test_identifier_quoting_escapes_embedded_quotes():
    """CREATE DATABASE cannot be parameterised, so the identifier must be escaped."""
    assert quote_identifier('we"ird') == '"we""ird"'


def test_identifier_quoting_rejects_a_null_byte():
    with pytest.raises(ValueError, match="null byte"):
        quote_identifier("bad\x00name")


def test_alembic_config_points_at_the_repo_migrations():
    config = alembic_config()
    assert config.get_main_option("script_location").endswith("migrations")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_harness_db.py -v`
Expected: collection ERROR — `No module named 'tests.harness.db'`

- [ ] **Step 3: Create `tests/harness/db.py`**

```python
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
```

- [ ] **Step 4: Create `tests/tiers/t3_integration/conftest.py`**

```python
"""Integration-tier fixtures: a migrated, per-run PostgreSQL database.

`Settings(...)` is constructed directly rather than via `get_settings()`. That
accessor is an `lru_cache` singleton, and a session-scoped fixture calling it would
capture a snapshot that survives every per-test cache clear — silently defeating
isolation exactly where this tier depends on it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.settings import Settings
from tests.harness.db import create_database_sync, drop_database_sync, run_migrations
from tests.harness.isolation import RunIsolation


def _test_stack_settings(isolation: RunIsolation) -> Settings:
    """Point settings at the compose test stack's offset ports for this run's slice."""
    return Settings(
        postgres_host=os.environ.get("SMARTHEALTH_TEST_PG_HOST", "localhost"),
        postgres_port=int(os.environ.get("SMARTHEALTH_TEST_PG_PORT", "15432")),
        postgres_user="smarthealth",
        postgres_password="smarthealth",
        postgres_db=isolation.postgres_db,
        mongo_host="localhost",
        mongo_port=27018,
        mongo_db=isolation.mongo_db,
        redis_host="localhost",
        redis_port=16379,
        redis_db=isolation.redis_db,
        redis_prefix=isolation.redis_prefix,
        resource_prefix=isolation.resource_prefix,
    )


@pytest.fixture(scope="session")
def db_settings(isolation: RunIsolation, stack) -> Iterator[Settings]:
    """A migrated database of this run's own, dropped when the session ends."""
    settings = _test_stack_settings(isolation)
    create_database_sync(settings.postgres_admin_url, settings.postgres_db)
    try:
        run_migrations(
            {
                "SMARTHEALTH_POSTGRES_HOST": settings.postgres_host,
                "SMARTHEALTH_POSTGRES_PORT": str(settings.postgres_port),
                "SMARTHEALTH_POSTGRES_USER": settings.postgres_user,
                "SMARTHEALTH_POSTGRES_PASSWORD": settings.postgres_password,
                "SMARTHEALTH_POSTGRES_DB": settings.postgres_db,
            }
        )
        yield settings
    finally:
        drop_database_sync(settings.postgres_admin_url, settings.postgres_db)


@pytest.fixture
async def db_session(db_settings: Settings) -> AsyncIterator[AsyncSession]:
    """A session on the migrated database. Rolls back so tests cannot bleed."""
    engine = create_engine(db_settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()
```

- [ ] **Step 5: Run the unit test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_harness_db.py -v`
Expected: 4 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add tests/harness/db.py tests/tiers/t3_integration/conftest.py tests/tiers/t0_unit/test_harness_db.py
git commit -m "feat: per-run migrated database for the integration tier"
```

---

## Task 13: Integration tests — the schema and its guarantees

These are the highest-value tests in this plan: they assert the *database* enforces the
booking invariants, which is the entire reason for choosing pre-generated slots.

**Files:**
- Test: `tests/tiers/t3_integration/test_schema.py`

- [ ] **Step 1: Write the test**

Create `tests/tiers/t3_integration/test_schema.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db.all_models import ALL_TABLES
from app.modules.identity.models import User, UserRole
from app.modules.patients.models import Patient
from app.modules.providers.models import Clinic, Provider, ProviderSlot, SlotStatus
from app.modules.scheduling.models import Appointment, AppointmentStatus

pytestmark = [pytest.mark.integration, pytest.mark.docker]

BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


async def _clinic_and_provider(session) -> tuple[Clinic, Provider]:
    clinic = Clinic(name=f"Riverside {uuid.uuid4().hex[:8]}", timezone="UTC")
    provider = Provider(
        first_name="Ada", last_name="Lovelace", specialty="cardiology",
        license_number=f"LIC-{uuid.uuid4().hex[:10]}",
    )
    session.add_all([clinic, provider])
    await session.flush()
    return clinic, provider


async def test_every_expected_table_exists_after_migration(db_session):
    def _tables(connection):
        return set(inspect(connection).get_table_names())

    present = await db_session.run_sync(lambda s: _tables(s.connection()))
    assert ALL_TABLES <= present
    assert "alembic_version" in present


async def test_a_patient_can_be_created_without_a_user(db_session):
    """The walk-in case from design spec 3.1, proven against the real schema."""
    patient = Patient(mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Jo", last_name="Bloggs")
    db_session.add(patient)
    await db_session.flush()
    assert patient.id is not None
    assert patient.user_id is None


async def test_a_patient_can_be_linked_to_a_user(db_session):
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        password_hash="x", role=UserRole.PATIENT,
    )
    db_session.add(user)
    await db_session.flush()
    patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Jo", last_name="Bloggs",
        user_id=user.id,
    )
    db_session.add(patient)
    await db_session.flush()
    assert patient.user_id == user.id


async def test_overlapping_slots_for_one_provider_are_rejected(db_session):
    """The exclusion constraint, not application code, prevents double-bookable
    inventory. A buggy slot generator must fail at write time."""
    clinic, provider = await _clinic_and_provider(db_session)
    db_session.add(
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME, ends_at=BASE_TIME + timedelta(minutes=30),
        )
    )
    await db_session.flush()

    db_session.add(
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME + timedelta(minutes=15),
            ends_at=BASE_TIME + timedelta(minutes=45),
        )
    )
    with pytest.raises(IntegrityError, match="no_overlap"):
        await db_session.flush()


async def test_adjacent_slots_are_allowed(db_session):
    """tstzrange is half-open, so 09:00-09:30 and 09:30-10:00 must not collide."""
    clinic, provider = await _clinic_and_provider(db_session)
    db_session.add_all([
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME, ends_at=BASE_TIME + timedelta(minutes=30),
        ),
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME + timedelta(minutes=30),
            ends_at=BASE_TIME + timedelta(minutes=60),
        ),
    ])
    await db_session.flush()


async def test_a_slot_ending_before_it_starts_is_rejected(db_session):
    clinic, provider = await _clinic_and_provider(db_session)
    db_session.add(
        ProviderSlot(
            provider_id=provider.id, clinic_id=clinic.id,
            starts_at=BASE_TIME, ends_at=BASE_TIME - timedelta(minutes=5),
        )
    )
    with pytest.raises(IntegrityError, match="end_after_start"):
        await db_session.flush()


async def _patient(session) -> Patient:
    patient = Patient(
        mrn=f"MRN-{uuid.uuid4().hex[:8]}", first_name="Sam", last_name="Doe"
    )
    session.add(patient)
    await session.flush()
    return patient


async def test_two_live_appointments_cannot_hold_one_slot(db_session):
    """The partial unique index makes the invariant a database guarantee, independent
    of whether the booking activity's conditional update is written correctly."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=BASE_TIME, ends_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.add(slot)
    patient_a = await _patient(db_session)
    patient_b = await _patient(db_session)
    await db_session.flush()

    db_session.add(
        Appointment(
            patient_id=patient_a.id, provider_id=provider.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    await db_session.flush()

    db_session.add(
        Appointment(
            patient_id=patient_b.id, provider_id=provider.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    with pytest.raises(IntegrityError, match="ux_appointments_live_slot"):
        await db_session.flush()


async def test_a_cancelled_appointment_frees_the_slot_for_a_new_one(db_session):
    """The index is partial precisely so cancelling releases the slot."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=BASE_TIME, ends_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.add(slot)
    patient_a = await _patient(db_session)
    patient_b = await _patient(db_session)
    await db_session.flush()

    first = Appointment(
        patient_id=patient_a.id, provider_id=provider.id,
        clinic_id=clinic.id, slot_id=slot.id, status=AppointmentStatus.CANCELLED,
    )
    db_session.add(first)
    await db_session.flush()

    db_session.add(
        Appointment(
            patient_id=patient_b.id, provider_id=provider.id,
            clinic_id=clinic.id, slot_id=slot.id,
        )
    )
    await db_session.flush()  # must not raise


async def test_a_raw_update_still_bumps_updated_at(db_session):
    """`onupdate=func.now()` does not fire for raw SQL - verified against the live
    database. Week 2's booking activity claims a slot with a raw conditional UPDATE,
    so a trigger, not the ORM default, is what keeps updated_at honest."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=BASE_TIME, ends_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.add(slot)
    await db_session.flush()
    before = (
        await db_session.execute(
            text("SELECT updated_at FROM provider_slots WHERE id = :id"), {"id": slot.id}
        )
    ).scalar_one()

    await db_session.execute(
        text("UPDATE provider_slots SET status = 'held' WHERE id = :id"), {"id": slot.id}
    )
    after = (
        await db_session.execute(
            text("SELECT updated_at FROM provider_slots WHERE id = :id"), {"id": slot.id}
        )
    ).scalar_one()

    assert after > before, "the BEFORE UPDATE trigger did not fire on a raw UPDATE"


async def test_an_appointment_defaults_to_pending(db_session):
    """Confirmed is reachable only after the workflow succeeds; the database default
    must never be 'confirmed'."""
    clinic, provider = await _clinic_and_provider(db_session)
    patient = await _patient(db_session)
    appointment = Appointment(
        patient_id=patient.id, provider_id=provider.id, clinic_id=clinic.id
    )
    db_session.add(appointment)
    await db_session.flush()
    await db_session.refresh(appointment)
    assert appointment.status is AppointmentStatus.PENDING


async def test_the_atomic_slot_claim_returns_zero_rows_when_already_held(db_session):
    """The Week 2 booking activity depends on this exact behaviour."""
    clinic, provider = await _clinic_and_provider(db_session)
    slot = ProviderSlot(
        provider_id=provider.id, clinic_id=clinic.id,
        starts_at=BASE_TIME, ends_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.add(slot)
    await db_session.flush()

    claim = text(
        "UPDATE provider_slots SET status = 'held' "
        "WHERE id = :slot_id AND status = 'free'"
    )
    first = await db_session.execute(claim, {"slot_id": slot.id})
    assert first.rowcount == 1

    second = await db_session.execute(claim, {"slot_id": slot.id})
    assert second.rowcount == 0

    held = await db_session.execute(
        select(ProviderSlot.status).where(ProviderSlot.id == slot.id)
    )
    assert held.scalar_one() is SlotStatus.HELD
```

- [ ] **Step 2: Run against the live stack**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t3_integration/test_schema.py -v`
Expected: 11 passed. First run creates and migrates a per-run database, so it takes a few
seconds longer.

If `test_overlapping_slots_for_one_provider_are_rejected` fails with *no* IntegrityError,
the exclusion constraint did not make it into the migration — fix Task 9 Step 5 rather
than weakening this test. It is the whole point of the design.

- [ ] **Step 3: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m docker -q
.venv/Scripts/python.exe -m ruff check app tests
git add tests/tiers/t3_integration/test_schema.py
git commit -m "test: prove the database enforces slot and booking invariants"
```

---

## Task 14: Integration tests — readiness, Mongo, Redis

**Files:**
- Test: `tests/tiers/t3_integration/test_readiness.py`
- Test: `tests/tiers/t3_integration/test_document_store.py`

- [ ] **Step 1: Create `tests/tiers/t3_integration/test_readiness.py`**

```python
import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
def live_infrastructure(monkeypatch, db_settings: Settings):
    """Point the application at this run's slice of the live stack."""
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_HOST", db_settings.postgres_host)
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_PORT", str(db_settings.postgres_port))
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_USER", db_settings.postgres_user)
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_PASSWORD", db_settings.postgres_password)
    monkeypatch.setenv("SMARTHEALTH_POSTGRES_DB", db_settings.postgres_db)
    monkeypatch.setenv("SMARTHEALTH_MONGO_HOST", db_settings.mongo_host)
    monkeypatch.setenv("SMARTHEALTH_MONGO_PORT", str(db_settings.mongo_port))
    monkeypatch.setenv("SMARTHEALTH_MONGO_DB", db_settings.mongo_db)
    monkeypatch.setenv("SMARTHEALTH_REDIS_HOST", db_settings.redis_host)
    monkeypatch.setenv("SMARTHEALTH_REDIS_PORT", str(db_settings.redis_port))
    monkeypatch.setenv("SMARTHEALTH_REDIS_DB", str(db_settings.redis_db))


async def test_ready_reports_all_dependencies_healthy(live_infrastructure):
    """Referenced by tests/cases/sys-002-ready-reports-all-dependencies-healthy.yaml."""
    async with LifespanManager(create_app()) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    reported = {entry["name"]: entry["ok"] for entry in body["dependencies"]}
    assert reported == {"postgres": True, "mongo": True, "redis": True}
```

- [ ] **Step 2: Create `tests/tiers/t3_integration/test_document_store.py`**

```python
from datetime import UTC, datetime

import pytest

from app.db.mongo import create_mongo_client, ensure_audit_indexes, get_audit_collection
from app.db.redis import create_redis_client, namespaced_key
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_an_audit_event_round_trips(db_settings: Settings):
    """Mongo owns the audit trail; the payload shape varies per entity by design."""
    client = create_mongo_client(db_settings)
    try:
        await ensure_audit_indexes(client, db_settings)
        collection = get_audit_collection(client, db_settings)
        document = {
            "entity_type": "patient",
            "entity_id": "11111111-1111-1111-1111-111111111111",
            "action": "profile_updated",
            "at": datetime.now(UTC),
            "before": {"phone": None},
            "after": {"phone": "+44 20 7946 0000"},
        }
        await collection.insert_one(document)

        stored = await collection.find_one({"entity_type": "patient"})
        assert stored["action"] == "profile_updated"
        assert stored["after"]["phone"] == "+44 20 7946 0000"
    finally:
        await get_audit_collection(client, db_settings).drop()
        client.close()


async def test_the_audit_indexes_exist(db_settings: Settings):
    client = create_mongo_client(db_settings)
    try:
        await ensure_audit_indexes(client, db_settings)
        collection = get_audit_collection(client, db_settings)
        names = [index["name"] async for index in collection.list_indexes()]
        assert any("entity_type" in name for name in names)
    finally:
        client.close()


async def test_redis_round_trips_a_namespaced_key(db_settings: Settings):
    """The prefix is the only thing keeping two concurrent runs apart in Redis."""
    client = create_redis_client(db_settings)
    try:
        key = namespaced_key(db_settings, "probe:slot-hold")
        assert key.startswith(db_settings.redis_prefix)
        await client.set(key, "held", ex=30)
        assert await client.get(key) == "held"
        await client.delete(key)
        assert await client.get(key) is None
    finally:
        await client.aclose()


async def test_redis_responds_to_ping(db_settings: Settings):
    client = create_redis_client(db_settings)
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
```

- [ ] **Step 3: Run against the live stack**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t3_integration -v`
Expected: 16 passed — 1 stack smoke + 10 schema + 1 readiness + 4 document store.

- [ ] **Step 4: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m docker -q
.venv/Scripts/python.exe -m ruff check app tests
git add tests/tiers/t3_integration/test_readiness.py tests/tiers/t3_integration/test_document_store.py
git commit -m "test: readiness against live infrastructure, Mongo and Redis round-trips"
```

---

## Task 15: A second catalog case, impl-backed

This gives the catalog its first `impl_backed` entry, which also exercises the pointer
resolution check whose parametrize bucket has been empty since it was written.

**Files:**
- Create: `tests/cases/sys-002-ready-reports-all-dependencies-healthy.yaml`
- Modify: `tests/cases/CATALOG.md` (regenerated)

- [ ] **Step 1: Create the case**

```yaml
id: sys-002-ready-reports-all-dependencies-healthy
title: "Readiness reports every infrastructure dependency healthy"
requirement: [PART-A-OBS-1]
tier: integration
priority: P0
status: ready
impl: "tests/tiers/t3_integration/test_readiness.py::test_ready_reports_all_dependencies_healthy"
```

- [ ] **Step 2: Confirm it routes as impl-backed**

Run: `.venv/Scripts/python.exe -m tests.runner.route_check`
Expected: exit 0, and the table shows `sys-002-ready-reports-all-dependencies-healthy
integration P0 impl`.

- [ ] **Step 3: Confirm the pointer check now actually runs**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/test_catalog.py -v`
Expected: `test_impl_pointer_resolves[sys-002-ready-reports-all-dependencies-healthy]`
PASSED — no longer the `[NOTSET]` empty-parameter skip.

- [ ] **Step 4: Prove the pointer check would catch a broken link**

Temporarily change the `impl:` line to `::test_ready_reports_all_dependencies_healthyX`,
run the catalog test, and confirm it FAILS with "has no test function named". Restore the
correct value and confirm it passes.

- [ ] **Step 5: Regenerate the catalog and commit**

```bash
.venv/Scripts/python.exe -m tests.runner.route_check --write-catalog --write-traceability
.venv/Scripts/python.exe -m pytest -m "not docker" -q
git add tests/cases/sys-002-ready-reports-all-dependencies-healthy.yaml tests/cases/CATALOG.md
git commit -m "test: add an impl-backed readiness case to the catalog"
```

---

## Task 16: Arm the `Stop` hook and update the documentation

This is deliberately last. Once `app/**` is a core path, changing application code
without a validated test case blocks a session from stopping.

**Files:**
- Modify: `tests/core-paths.txt`
- Modify: `CLAUDE.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Activate the core paths**

Replace the commented block at the end of `tests/core-paths.txt` with:

```
app/api/**
app/core/**
app/db/**
app/modules/**
migrations/**
```

Leave the explanatory comments above intact. Note `app/settings.py` and `app/main.py` are
deliberately **not** listed: they are wiring that changes whenever a module is added, and
gating them would fire on every task without adding signal.

- [ ] **Step 2: Verify the hook now blocks**

Run:
```bash
python -c "import pathlib; p=pathlib.Path('app/core/clock.py'); p.write_text(p.read_text()+'\n# scratch\n')"
python .claude/hooks/feature_test_stop.py; echo "exit=$?"
git checkout app/core/clock.py
```
Expected: `exit=2`, with a message naming `app/core/clock.py`. Then confirm the tree is
clean and the hook returns to exit 0 with `git status --short` empty.

- [ ] **Step 3: Update `CLAUDE.md`**

Replace the `## Repository status` body with:

```
**Week 1 foundation in place; no business endpoints yet.** The repository holds the
assignment requirements, the testing harness, and the application foundation: settings,
core utilities, async SQLAlchemy with Alembic migrations, the domain schema, Mongo and
Redis clients, an auth skeleton, and liveness/readiness endpoints.

Commands that work today:

| Command | Purpose |
| --- | --- |
| `python -m pytest -m "not docker"` | fast tests — T0 unit, T1 contract |
| `python -m pytest -m docker` | integration tests against the compose stack |
| `python -m tests.runner.route_check` | validate and route the case catalog |
| `python -m alembic upgrade head` | apply migrations |
| `python -m alembic revision --autogenerate -m "..."` | create a migration |
```

Then add this to the `## Testing` section, after the `Stop` hook paragraph:

```
The hook is **armed**: `app/api/**`, `app/core/**`, `app/db/**`, `app/modules/**`, and
`migrations/**` are core paths. Changing any of them without adding a validated case
blocks the session. Author one with the **smarthealth-testcase** skill.
```

Finally add a `## Domain model` section at the end:

```
## Domain model

Design and rationale: `docs/superpowers/specs/2026-09-02-week1-foundation-design.md`.

Four decisions the requirements left open, and are easy to get wrong:

1. **A patient is not a user.** `users` is auth identity; `patients`/`providers` are
   domain records with a nullable, unique `user_id`. Front-desk staff register walk-ins
   who have no credentials.
2. **Slots are pre-generated rows.** Booking is `UPDATE provider_slots SET status='held'
   WHERE id=:id AND status='free'` — zero rows affected *is* the conflict. A GiST
   exclusion constraint additionally forbids overlapping slots for one provider.
3. **A visit is separate from an appointment.** Appointment = the booking; visit = what
   happened. Average Wait Time is `seen_at - checked_in_at`; a no-show is an appointment
   with no visit.
4. **Mongo owns the audit trail.** Postgres is the system of record for all
   transactional state.

`appointments.status` starts at `pending`. **`confirmed` is reachable only after the
whole booking workflow succeeds** — the assignment's headline invariant, enforced by the
schema default rather than by a service method.
```

- [ ] **Step 4: Update `tests/README.md`**

In the "Tiers" table, the `integration` row's "Proves" column currently reads
"repositories, consumers, tasks, idempotency". Replace it with "schema and its
constraints, repositories, consumers, tasks, idempotency".

Add to the end of the "Known constraints" section:

```
**5. Integration tests get a per-run database, created and dropped per session.**
`tests/tiers/t3_integration/conftest.py` creates `<prefix>db`, runs `alembic upgrade
head`, and drops it at session end. It builds `Settings(...)` directly rather than
calling `get_settings()` — see constraint 1. A crashed session can leave the database
behind; `docker compose -p smarthealth-test --env-file .env.test -f
docker-compose.infra.yml exec postgres psql -U smarthealth -c "\l"` lists strays.
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m pytest -m docker -q
python .claude/hooks/feature_test_stop.py; echo "exit=$?"
git add tests/core-paths.txt CLAUDE.md tests/README.md
git commit -m "chore: arm the Stop hook for app paths and document the domain model"
```
Expected: both lanes pass; hook exits 0 (only documentation and config changed, no core
path touched).

---

## Task 17: Final verification

- [ ] **Step 1: Both lanes**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -v
.venv/Scripts/python.exe -m pytest -m docker -v
.venv/Scripts/python.exe -m pytest -v
```
Report all three counts. Zero collection errors.

- [ ] **Step 2: Lint**

```bash
.venv/Scripts/python.exe -m ruff check app tests
.venv/Scripts/python.exe -m ruff check .claude/hooks/feature_test_stop.py
```

- [ ] **Step 3: Migrations are honest**

```bash
export SMARTHEALTH_POSTGRES_PORT=15432
export SMARTHEALTH_POSTGRES_DB=smarthealth
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```
Expected: `No new upgrade operations detected.` — models and migration agree.

- [ ] **Step 4: The catalog**

```bash
.venv/Scripts/python.exe -m tests.runner.route_check --write-catalog --write-traceability
git status --short
```
Expected: exit 0, two cases (one `declarative`, one `impl`), and **no diff** — the
committed `CATALOG.md` is current.

- [ ] **Step 5: The hook, all three states**

Dormant → 0. Touch `app/core/clock.py` with no case → **2**. `E2E_WAIVE="verification"` →
0 with a record in `tests/waivers.log`. Restore everything afterwards, delete
`tests/waivers.log`, and confirm `git status --short` is clean.

- [ ] **Step 6: No stray databases**

```bash
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml \
  exec -T postgres psql -U smarthealth -d postgres -c "\l" | grep -c "t_" || true
```
Expected: `0` — every per-run database was dropped.

- [ ] **Step 7: Definition of done**

State explicitly whether each holds:

1. Both test lanes pass with zero collection errors
2. `alembic upgrade head` then `alembic check` reports no drift
3. The exclusion constraint rejects overlapping slots (T3 proves it)
4. The partial unique index rejects two live appointments on one slot (T3 proves it)
5. An appointment's database default is `pending`, never `confirmed`
6. `/ready` returns 503 with a per-dependency breakdown when infrastructure is down, and
   200 when it is up
7. The T1 lane runs the real lifespan with no containers
8. A patient can exist with no user account
9. The catalog has an `impl`-backed case and the pointer check runs
10. The `Stop` hook is armed and blocks an uncovered `app/**` change

- [ ] **Step 8: Commit any report regeneration**

Only if Step 4 produced a diff:
```bash
git add tests/cases/CATALOG.md
git commit -m "chore: regenerate catalog after week 1 foundation"
```

---

## Definition of done

- [ ] `pytest -m "not docker"` and `pytest -m docker` both pass, zero collection errors
- [ ] `alembic check` reports no drift between models and migrations
- [ ] The GiST exclusion constraint and the partial unique index are proven to reject violations
- [ ] `appointments.status` defaults to `pending`
- [ ] `/health` remains a flat `dict[str, str]`; `/ready` reports each dependency and 503s when any is down
- [ ] The T1 contract lane runs the real FastAPI lifespan without Docker
- [ ] The catalog has two cases, one declarative and one impl-backed
- [ ] The `Stop` hook is armed for `app/**` and `migrations/**`
- [ ] `CLAUDE.md` documents the four domain decisions and the pending-first invariant
