# Week 1 Management APIs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the endpoints Week 1 names as deliverables — login, patient management, provider management — with audit writes, a `create-user` CLI, and a containerised app.

**Architecture:** `router → service → SQLAlchemy`. Services never import FastAPI; they raise domain errors that exception handlers translate, so Week 2's Temporal activities can call the same functions. Every mutation writes an awaited audit document to Mongo.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 async · PyJWT · pwdlib[argon2] · pymongo async · pytest (five-tier harness)

**Spec:** `docs/superpowers/specs/2026-09-03-week1-management-apis-design.md`

---

## Conventions every task must follow

- Run Python via `.venv/Scripts/python.exe`. The ambient `python` lacks the dependencies.
- Ruff: `line-length = 100`, `select = ["E4","E7","E9","F","E501","B"]`. Imports at the top (E402). **B is on** — never `pytest.raises(Exception)`.
- Every task ends with `ruff check app tests` clean and the fast lane passing.
- Stage explicit paths. **Never `git add -A`** — a shared index has already caused one race in this repo.
- The Docker stack is running; do not tear it down. Postgres **15432**, Mongo **27018**, Redis **16379**, all credentials `smarthealth`.
- Alembic needs connection settings: `SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth`.

### The Stop hook is armed

`app/api/**`, `app/core/**`, `app/db/**`, `app/modules/**` and `migrations/**` are core paths. A session that changes one without adding a validated case file cannot stop.

Endpoint tasks (4, 6, 8) author case files. **Plumbing tasks** (1, 2, 3, 11, 12) add no endpoint behaviour, so if the hook blocks, the correct response is a waiver with a specific reason — not disabling the gate:

```bash
E2E_WAIVE="core/errors.py is plumbing; behaviour covered by the endpoint cases in task 6"
```

The reason is logged to `tests/waivers.log`, which is tracked on purpose — an audit trail that isn't committed isn't an audit trail.

## Findings from execution that later tasks MUST honour

Discovered while implementing tasks 1-4. Each one silently breaks a later task if ignored.

1. **`.test` email addresses are rejected.** `email-validator` refuses `.test`, `.invalid`
   and `localhost` as special-use TLDs, so `nobody@example.test` returns 422 before the
   service is ever called. **Task 9 fixtures and every case file must use `example.com`.**
   A `.test` address would look exactly like an auth bug.
2. **`app.routes` does not enumerate routes on FastAPI 0.141.1.** Mounted routers appear as
   a single `fastapi.routing._IncludedRouter` exposing `original_router` /
   `effective_route_contexts`, with no `.routes` to recurse into. Any meta-test that walks
   `app.routes` will find zero routes and **pass vacuously**. Write it against that
   structure, and prove it discriminates by mutation before trusting it.
3. **`/openapi.json` cannot prove a route is public.** `require_role` reads the raw
   `Authorization` header instead of using a FastAPI security scheme, so it contributes
   nothing to the schema — a gated route and a public route are indistinguishable there.
   Prove "public" behaviourally: no auth header plus an invalid body returns **422**, not
   401, because validation only runs once no auth gate has rejected the request first.
4. **`app/core/*` modules must not import FastAPI.** Three separate modules shipped this
   defect (`errors.py`, `audit.py`, `pagination.py`). The rule: framework-free types live in
   `app/core/`, and every FastAPI dependency provider lives in `app/api/deps.py`. Prove it in
   a **fresh interpreter** — an in-process assertion cannot, since pytest has already
   imported FastAPI:
   `python -c "import sys; import app.core.X; print('fastapi' in sys.modules)"` must be `False`.
5. **`get_audit_log` is exercised by nothing** until tasks 6 and 8 call it. Its
   `request.app.state.mongo` attribute names and its argument order into
   `get_audit_collection` are verified only by reading. **Task 9 must drive it through the
   real dependency**, not a test override, or the wiring stays unproven.
6. **The duplicate-MRN check in Task 5 matches the wrong constraint name.** `patients.mrn`
   is declared `unique=True, index=True`, so SQLAlchemy emits a **unique index**
   `ix_patients_mrn` (see `migrations/versions/0001_baseline.py:74`) and **no**
   `uq_patients_mrn` exists. The plan's `if "uq_patients_mrn" in str(exc.orig)` therefore
   never matches, and a duplicate MRN returns **500 instead of the required 409**.
   `providers.license_number` is declared `unique=True` only, so it really does get
   `uq_providers_license_number` — the two are asymmetric. Do not string-match at all:
   asyncpg raises `UniqueViolationError` carrying a `.constraint_name` attribute, so use
   `getattr(exc.orig, "constraint_name", None)`, and **confirm the observed value by
   provoking a real duplicate insert** before writing the comparison.
7. **The default `jwt_secret` is 20 bytes**, so PyJWT emits `InsecureKeyLengthWarning` on
   every token operation. Raise the default to >=32 bytes in Task 12.

## Deviation from the spec, deliberate

The spec's §8 says T1 overrides the service dependency with a fake. That turns out to be unnecessary: `require_role` and request validation both reject **before** the handler body runs, and `get_session` opens a session without connecting (the engine is lazy). So the T1 authz matrix and validation tests need neither a database nor a fake. Response *shape* on the success path is proven at T3 against real data, where it means more.

## File Structure

| File | Responsibility |
| --- | --- |
| `app/core/errors.py` | `DomainError` hierarchy carrying `status_code`, plus the handler registration |
| `app/core/pagination.py` | `PageParams` dependency and the generic `Page[T]` response |
| `app/core/audit.py` | `AuditEvent`, `AuditLog`, and the `get_audit_log` dependency |
| `app/modules/identity/schemas.py` | `LoginRequest`, `TokenResponse` |
| `app/modules/identity/service.py` | `authenticate` |
| `app/modules/identity/router.py` | `POST /auth/login` |
| `app/modules/patients/schemas.py` | `PatientCreate`, `PatientUpdate`, `PatientRead` |
| `app/modules/patients/service.py` | register / get / list / update, audit emission |
| `app/modules/patients/router.py` | the four patient routes |
| `app/modules/providers/schemas.py` | `ProviderCreate`, `ProviderUpdate`, `ProviderRead` |
| `app/modules/providers/service.py` | register / get / list / update, audit emission |
| `app/modules/providers/router.py` | the four provider routes |
| `app/cli.py` | `create-user` |
| `Dockerfile`, `docker-entrypoint.sh` | container image and startup |
| `app/api/router.py` | modified: mounts the three new routers |
| `app/main.py` | modified: registers error handlers |

---

## Task 1: Domain errors

**Files:**
- Create: `app/core/errors.py`
- Modify: `app/main.py`
- Test: `tests/tiers/t0_unit/test_errors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_errors.py`:

```python
import pytest

from app.core.errors import Conflict, DomainError, NotFound, PermissionDenied

pytestmark = pytest.mark.unit


def test_each_error_carries_its_http_status():
    assert NotFound("x").status_code == 404
    assert Conflict("x").status_code == 409
    assert PermissionDenied("x").status_code == 403


def test_the_detail_is_preserved():
    error = NotFound("patient 123 does not exist")
    assert error.detail == "patient 123 does not exist"
    assert str(error) == "patient 123 does not exist"


def test_every_domain_error_subclasses_the_base():
    """Starlette resolves handlers by walking the MRO, so one handler on the base
    class catches all of them — but only if they actually inherit from it."""
    for error_type in (NotFound, Conflict, PermissionDenied):
        assert issubclass(error_type, DomainError)


def test_the_base_class_is_not_raised_directly():
    """A bare DomainError has no meaningful status; it exists to be subclassed."""
    assert DomainError.status_code == 500
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_errors.py -v`
Expected: collection ERROR — `No module named 'app.core.errors'`

- [ ] **Step 3: Create `app/core/errors.py`**

```python
"""Domain errors and their HTTP translation.

Services raise these; routers never catch them. That separation is the reason the
service layer exists at all — Week 2's Temporal activities call the same functions and
need an exception they can act on, not an `HTTPException` that only means something to
a web framework.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    """A business-rule failure. Subclasses carry the status they translate to."""

    status_code = 500

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class PermissionDenied(DomainError):
    status_code = 403


class InvalidCredentials(DomainError):
    """Authentication failed. Deliberately says nothing about which part failed."""

    status_code = 401


def register_error_handlers(app: FastAPI) -> None:
    """One handler on the base class covers every subclass.

    Starlette resolves handlers by walking the raised exception's MRO, so this single
    registration catches `NotFound`, `Conflict` and the rest.
    """

    @app.exception_handler(DomainError)
    async def _handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": type(exc).__name__, "detail": exc.detail},
        )
```

- [ ] **Step 4: Register the handlers in `app/main.py`**

Add to the import block:

```python
from app.core.errors import register_error_handlers
```

and inside `create_app`, after `application.include_router(api_router)`:

```python
    register_error_handlers(application)
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_errors.py -v`
Expected: 4 passed

- [ ] **Step 6: Prove the handler actually translates**

The unit tests cover the classes; nothing yet proves Starlette routes them. In a throwaway
snippet (do not commit), build a small FastAPI app with `register_error_handlers`, add a
route that raises `NotFound("gone")`, call it via `httpx.ASGITransport`, and confirm you
get **404** with body `{"error": "NotFound", "detail": "gone"}`. Repeat for `Conflict`
(409). Paste both responses.

- [ ] **Step 7: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/core/errors.py app/main.py tests/tiers/t0_unit/test_errors.py
git commit -m "feat: domain errors that translate to HTTP without services knowing"
```

---

## Task 2: Pagination

**Files:**
- Create: `app/core/pagination.py`
- Test: `tests/tiers/t0_unit/test_pagination.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_pagination.py`:

```python
import pytest
from pydantic import BaseModel, ValidationError

from app.core.pagination import MAX_LIMIT, Page, PageParams

pytestmark = pytest.mark.unit


class _Item(BaseModel):
    name: str


def test_page_params_hold_limit_and_offset():
    params = PageParams(limit=25, offset=50)
    assert params.limit == 25
    assert params.offset == 50


def test_a_page_reports_the_total_not_just_the_slice():
    """Without a total a client cannot tell whether more rows exist."""
    page = Page[_Item](
        items=[_Item(name="a")], total=137, limit=50, offset=100
    )
    assert page.total == 137
    assert len(page.items) == 1
    assert page.offset == 100


def test_the_maximum_limit_is_bounded():
    """A single request must not be able to ask for every row in the table."""
    assert MAX_LIMIT == 200


def test_a_page_rejects_a_negative_total():
    with pytest.raises(ValidationError):
        Page[_Item](items=[], total=-1, limit=50, offset=0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_pagination.py -v`
Expected: collection ERROR — `No module named 'app.core.pagination'`

- [ ] **Step 3: Create `app/core/pagination.py`**

```python
"""Offset pagination for list endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

#: A single request must not be able to ask the database for every row.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> PageParams:
    """FastAPI dependency for the two query parameters."""
    return PageParams(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    """One slice of a result set, plus enough context to fetch the next."""

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_pagination.py -v`
Expected: 4 passed

- [ ] **Step 5: Confirm the bounds are enforced by FastAPI, not just declared**

In a throwaway snippet, mount a route depending on `page_params`, then call it with
`?limit=500` and with `?limit=0` and confirm both return **422**. Also confirm `?limit=200`
succeeds. Paste the three status codes — `Query(le=MAX_LIMIT)` is only a real bound if
FastAPI rejects the request.

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/core/pagination.py tests/tiers/t0_unit/test_pagination.py
git commit -m "feat: bounded offset pagination with a total-carrying page"
```

---

## Task 3: Audit log

**Files:**
- Create: `app/core/audit.py`
- Test: `tests/tiers/t0_unit/test_audit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_audit.py`:

```python
from datetime import UTC, datetime

import pytest

from app.core.audit import AuditEvent, AuditLog
from app.core.clock import FixedClock

pytestmark = pytest.mark.unit

INSTANT = datetime(2026, 9, 3, 10, 30, tzinfo=UTC)


class _FakeCollection:
    """Records what would have been written, so the unit tier needs no Mongo."""

    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def insert_one(self, document: dict) -> None:
        self.documents.append(document)


async def test_record_writes_one_document_with_the_clock_timestamp():
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(
            entity_type="patient",
            entity_id="p1",
            action="registered",
            actor_user_id="u1",
            before=None,
            after={"mrn": "MRN-1"},
        )
    )

    assert len(collection.documents) == 1
    document = collection.documents[0]
    assert document["entity_type"] == "patient"
    assert document["entity_id"] == "p1"
    assert document["action"] == "registered"
    assert document["actor_user_id"] == "u1"
    assert document["before"] is None
    assert document["after"] == {"mrn": "MRN-1"}
    assert document["at"] == INSTANT


async def test_an_unattributed_change_is_allowed_but_explicit():
    """A CLI or background job has no acting user; the field is None, not missing."""
    collection = _FakeCollection()
    log = AuditLog(collection, FixedClock(INSTANT))

    await log.record(
        AuditEvent(entity_type="user", entity_id="u9", action="created")
    )

    assert collection.documents[0]["actor_user_id"] is None
    assert "actor_user_id" in collection.documents[0]


async def test_before_and_after_default_to_none():
    event = AuditEvent(entity_type="patient", entity_id="p1", action="viewed")
    assert event.before is None
    assert event.after is None


def test_an_audit_event_is_immutable():
    """An event that could be mutated after construction is not a record of anything."""
    event = AuditEvent(entity_type="patient", entity_id="p1", action="registered")
    with pytest.raises(AttributeError):
        event.action = "tampered"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_audit.py -v`
Expected: collection ERROR — `No module named 'app.core.audit'`

- [ ] **Step 3: Create `app/core/audit.py`**

```python
"""Audit trail writes.

Mongo owns the audit trail: append-only, high volume, and a before/after payload whose
shape differs per entity and per action (foundation spec section 3.4).

Writes are **awaited**, not fire-and-forget. A dropped audit entry is invisible and
unrecoverable; a failed request is neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Request

from app.core.clock import Clock, get_clock
from app.db.mongo import get_audit_collection


@dataclass(frozen=True)
class AuditEvent:
    """One recorded change. Frozen: a mutable record is not a record."""

    entity_type: str
    entity_id: str
    action: str
    actor_user_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class _Collection(Protocol):
    """The slice of a Mongo collection this module uses, so tests can fake it."""

    async def insert_one(self, document: dict[str, Any]) -> Any: ...


class AuditLog:
    def __init__(self, collection: _Collection, clock: Clock) -> None:
        self._collection = collection
        self._clock = clock

    async def record(self, event: AuditEvent) -> None:
        await self._collection.insert_one(
            {
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "action": event.action,
                "actor_user_id": event.actor_user_id,
                "before": event.before,
                "after": event.after,
                "at": self._clock.now(),
            }
        )


def get_audit_log(request: Request) -> AuditLog:
    """FastAPI dependency. Services receive an `AuditLog`, never the request."""
    settings = request.app.state.settings
    collection = get_audit_collection(request.app.state.mongo, settings)
    return AuditLog(collection, get_clock())
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_audit.py -v`
Expected: 4 passed

- [ ] **Step 5: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/core/audit.py tests/tiers/t0_unit/test_audit.py
git commit -m "feat: awaited audit writes with an injectable collection"
```

---

## Task 4: Login

**Files:**
- Create: `app/modules/identity/schemas.py`, `app/modules/identity/service.py`, `app/modules/identity/router.py`
- Modify: `app/api/router.py`
- Test: `tests/tiers/t1_contract/test_login_contract.py`
- Create: `tests/cases/aut-001-login-issues-a-token.yaml`

- [ ] **Step 1: Write the failing contract test**

Create `tests/tiers/t1_contract/test_login_contract.py`:

```python
import pytest

pytestmark = pytest.mark.contract


async def test_login_requires_email_and_password(api_client):
    response = await api_client.post("/auth/login", json={})
    assert response.status_code == 422


async def test_login_rejects_a_malformed_email(api_client):
    response = await api_client.post(
        "/auth/login", json={"email": "not-an-email", "password": "x"}
    )
    assert response.status_code == 422


async def test_login_is_public(api_client):
    """No Authorization header: the route must not be behind require_role."""
    response = await api_client.post(
        "/auth/login", json={"email": "nobody@example.test", "password": "wrong"}
    )
    assert response.status_code != 401 or response.json()["error"] == "InvalidCredentials"
    assert response.status_code in (401, 500)
```

Note the third test: without a database it cannot reach a successful login, but it *can*
prove the route exists and is not gated by `require_role` (which would give a 401 with a
`WWW-Authenticate` header rather than a domain error). Real login behaviour is Task 9's
integration test.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_login_contract.py -v`
Expected: all fail with 404 — the route does not exist.

- [ ] **Step 3: Create `app/modules/identity/schemas.py`**

```python
"""Request and response bodies for authentication."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
```

`EmailStr` needs `email-validator`, which FastAPI pulls in via `pydantic[email]`. If the
import fails, add `"pydantic[email]>=2.9"` to `pyproject.toml` dependencies and report it.

- [ ] **Step 4: Create `app/modules/identity/service.py`**

```python
"""Authentication.

No FastAPI import: Week 2's Temporal activities call these functions directly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidCredentials
from app.modules.identity.models import User
from app.modules.identity.security import create_access_token, verify_password
from app.settings import Settings


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    settings: Settings,
    now: datetime,
) -> tuple[str, int]:
    """Return an access token and its lifetime in seconds.

    Every failure path raises the same error with the same message. Distinguishing
    "no such account" from "wrong password" tells an attacker which addresses are
    registered, and an inactive account should not be enumerable either.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if (
        user is None
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
        raise InvalidCredentials("email or password is incorrect")

    token = create_access_token(
        subject=str(user.id), role=user.role, settings=settings, now=now
    )
    return token, settings.jwt_expiry_minutes * 60
```

- [ ] **Step 5: Create `app/modules/identity/router.py`**

```python
"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, get_clock
from app.db.session import get_session
from app.modules.identity.schemas import LoginRequest, TokenResponse
from app.modules.identity.service import authenticate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> TokenResponse:
    token, expires_in = await authenticate(
        session,
        email=body.email,
        password=body.password,
        settings=request.app.state.settings,
        now=clock.now(),
    )
    return TokenResponse(access_token=token, expires_in=expires_in)
```

- [ ] **Step 6: Mount it in `app/api/router.py`**

```python
"""Router assembly. Every module's router is mounted here, and only here."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health
from app.modules.identity import router as identity_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(identity_router.router)
```

- [ ] **Step 7: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_login_contract.py -v`
Expected: 3 passed

**No case file in this task.** A case whose `impl` pointer names a test that does not
exist yet would fail `test_impl_pointer_resolves`. All three case files land in Task 9,
alongside the tests they point at.

- [ ] **Step 8: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/identity/schemas.py app/modules/identity/service.py \
        app/modules/identity/router.py app/api/router.py \
        tests/tiers/t1_contract/test_login_contract.py
git commit -m "feat: login endpoint issuing a bearer token"
```

If the hook blocks (core paths changed, case not yet valid), waive with:
`E2E_WAIVE="login case aut-001 lands with its integration test in task 9"`.

---

## Task 5: Patient schemas and service

**Files:**
- Create: `app/modules/patients/schemas.py`, `app/modules/patients/service.py`
- Test: `tests/tiers/t0_unit/test_patient_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_patient_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from app.modules.patients.schemas import PatientCreate, PatientUpdate

pytestmark = pytest.mark.unit


def test_create_requires_identifying_fields():
    with pytest.raises(ValidationError):
        PatientCreate(first_name="Jo")


def test_create_accepts_the_minimum():
    patient = PatientCreate(mrn="MRN-1", first_name="Jo", last_name="Bloggs")
    assert patient.date_of_birth is None
    assert patient.phone is None


def test_create_rejects_a_blank_mrn():
    with pytest.raises(ValidationError):
        PatientCreate(mrn="   ", first_name="Jo", last_name="Bloggs")


def test_create_rejects_a_malformed_email():
    with pytest.raises(ValidationError):
        PatientCreate(
            mrn="MRN-1", first_name="Jo", last_name="Bloggs", email="nope"
        )


def test_update_allows_a_single_field():
    update = PatientUpdate(phone="+44 20 7946 0000")
    assert update.phone == "+44 20 7946 0000"
    assert update.first_name is None


def test_update_reports_only_the_fields_actually_sent():
    """A PATCH that set every unspecified field to None would erase data."""
    update = PatientUpdate(phone="+44 20 7946 0000")
    assert update.model_dump(exclude_unset=True) == {"phone": "+44 20 7946 0000"}


def test_update_rejects_an_empty_body():
    with pytest.raises(ValidationError, match="at least one field"):
        PatientUpdate()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_patient_schemas.py -v`
Expected: collection ERROR — `No module named 'app.modules.patients.schemas'`

- [ ] **Step 3: Create `app/modules/patients/schemas.py`**

```python
"""Patient request and response bodies."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

NonBlank = Field(min_length=1)


class PatientCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    mrn: str = Field(min_length=1, max_length=32)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None


class PatientUpdate(BaseModel):
    """A partial update. Every field is optional, but the body must not be empty."""

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> PatientUpdate:
        if not self.model_dump(exclude_unset=True):
            raise ValueError("a patient update must set at least one field")
        return self


class PatientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mrn: str
    first_name: str
    last_name: str
    date_of_birth: date | None
    phone: str | None
    email: str | None
    user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
```

`str_strip_whitespace=True` is what makes the blank-MRN test fail validation: `"   "`
strips to `""`, which then violates `min_length=1`.

- [ ] **Step 4: Create `app/modules/patients/service.py`**

```python
"""Patient management.

No FastAPI import — Week 2's Temporal activities call these directly. Mutations write
an audit document before returning, awaited so a Mongo outage fails loudly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound
from app.core.pagination import PageParams
from app.modules.patients.models import Patient
from app.modules.patients.schemas import PatientCreate, PatientUpdate

ENTITY = "patient"

#: The database enforces MRN uniqueness with a unique index, not a unique constraint —
#: `Patient.mrn` is declared `unique=True, index=True`. Verified against
#: `migrations/versions/0001_baseline.py`.
MRN_UNIQUE_CONSTRAINT = "ix_patients_mrn"


def _snapshot(patient: Patient) -> dict[str, object]:
    """The audited shape of a patient. Deliberately excludes nothing sensitive today,
    but is a single place to redact from if that changes."""
    return {
        "mrn": patient.mrn,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": patient.date_of_birth.isoformat()
        if patient.date_of_birth
        else None,
        "phone": patient.phone,
        "email": patient.email,
    }


async def register_patient(
    session: AsyncSession,
    audit: AuditLog,
    *,
    data: PatientCreate,
    actor_user_id: str | None,
) -> Patient:
    patient = Patient(**data.model_dump())
    session.add(patient)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The naming convention makes this constraint name predictable.
        # `patients.mrn` is `unique=True, index=True`, so the database enforces it with a
        # unique INDEX (`ix_patients_mrn`), not a named unique constraint. Compare against
        # asyncpg's `constraint_name` rather than string-matching the message.
        if getattr(exc.orig, "constraint_name", None) == MRN_UNIQUE_CONSTRAINT:
            raise Conflict(f"a patient with MRN {data.mrn} already exists") from exc
        raise

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(patient.id),
            action="registered",
            actor_user_id=actor_user_id,
            after=_snapshot(patient),
        )
    )
    return patient


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise NotFound(f"patient {patient_id} does not exist")
    return patient


async def list_patients(
    session: AsyncSession, *, page: PageParams, search: str | None = None
) -> tuple[list[Patient], int]:
    """Return one page of patients and the total matching the same filter."""
    conditions = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                Patient.mrn.ilike(pattern),
                Patient.first_name.ilike(pattern),
                Patient.last_name.ilike(pattern),
            )
        )

    total = await session.scalar(
        select(func.count()).select_from(Patient).where(*conditions)
    )
    rows = await session.scalars(
        select(Patient)
        .where(*conditions)
        .order_by(Patient.created_at.desc(), Patient.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)


async def update_patient(
    session: AsyncSession,
    audit: AuditLog,
    *,
    patient_id: uuid.UUID,
    data: PatientUpdate,
    actor_user_id: str | None,
) -> Patient:
    patient = await get_patient(session, patient_id)
    before = _snapshot(patient)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(patient, field, value)
    await session.flush()

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(patient.id),
            action="profile_updated",
            actor_user_id=actor_user_id,
            before=before,
            after=_snapshot(patient),
        )
    )
    return patient
```

The ordering in `list_patients` includes `Patient.id` as a tiebreaker. Ordering by
`created_at` alone is unstable when two rows share a timestamp, which would make
pagination skip or repeat rows.

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_patient_schemas.py -v`
Expected: 7 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/patients/schemas.py app/modules/patients/service.py \
        tests/tiers/t0_unit/test_patient_schemas.py
git commit -m "feat: patient schemas and service with audited mutations"
```

Waive if blocked: `E2E_WAIVE="patient service; behaviour cases land with the router in task 6"`.

---

## Task 6: Patient router

**Files:**
- Create: `app/modules/patients/router.py`
- Modify: `app/api/router.py`
- Test: `tests/tiers/t1_contract/test_patients_authz.py`

- [ ] **Step 1: Write the failing authz test**

Create `tests/tiers/t1_contract/test_patients_authz.py`:

```python
from datetime import UTC, datetime

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings(jwt_secret="dev-secret-change-me", jwt_expiry_minutes=30)
NOW = datetime.now(UTC)

VALID_BODY = {"mrn": "MRN-AUTHZ", "first_name": "Jo", "last_name": "Bloggs"}


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject="11111111-1111-1111-1111-111111111111",
        role=role, settings=SETTINGS, now=NOW,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_registering_a_patient_requires_a_token(api_client):
    response = await api_client.post("/patients", json=VALID_BODY)
    assert response.status_code == 401


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.PROVIDER])
async def test_a_patient_or_provider_may_not_register_patients(api_client, role):
    """Front-desk staff and admins register patients; clinicians do not."""
    response = await api_client.post("/patients", json=VALID_BODY, headers=auth(role))
    assert response.status_code == 403


@pytest.mark.parametrize("role", [UserRole.PATIENT])
async def test_a_patient_may_not_list_patients(api_client, role):
    response = await api_client.get("/patients", headers=auth(role))
    assert response.status_code == 403


async def test_listing_requires_a_token(api_client):
    response = await api_client.get("/patients")
    assert response.status_code == 401


async def test_updating_requires_a_token(api_client):
    response = await api_client.patch(
        "/patients/11111111-1111-1111-1111-111111111111", json={"phone": "x"}
    )
    assert response.status_code == 401


async def test_a_provider_may_not_update_a_patient(api_client):
    response = await api_client.patch(
        "/patients/11111111-1111-1111-1111-111111111111",
        json={"phone": "x"}, headers=auth(UserRole.PROVIDER),
    )
    assert response.status_code == 403


async def test_an_invalid_body_is_rejected_before_authorisation_matters(api_client):
    """422 for a bad body even with a permitted role — validation is not a bypass."""
    response = await api_client.post(
        "/patients", json={"first_name": "Jo"}, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422


async def test_an_out_of_range_limit_is_rejected(api_client):
    response = await api_client.get(
        "/patients?limit=500", headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422
```

These need no database: `require_role` and request validation both reject before the
handler body runs, and the session dependency opens without connecting.

**Note on `SETTINGS`:** it uses the same default `jwt_secret` the application will read,
so tokens minted here verify. If that proves fragile, override `get_token_settings` as
`tests/tiers/t1_contract/test_authz.py` does and say so in your report.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_patients_authz.py -v`
Expected: failures with 404 — the routes do not exist.

- [ ] **Step 3: Create `app/modules/patients/router.py`**

```python
"""Patient management routes.

Front-desk staff register and update patients — the same requirement that made
`patients.user_id` nullable. Clinicians may read but not write.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_audit_log, page_params
from app.core.audit import AuditLog
from app.core.pagination import Page, PageParams
from app.db.session import get_session
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.patients import service
from app.modules.patients.schemas import PatientCreate, PatientRead, PatientUpdate

router = APIRouter(prefix="/patients", tags=["patients"])

WRITERS = (UserRole.FRONT_DESK, UserRole.ADMIN)
READERS = (UserRole.FRONT_DESK, UserRole.ADMIN, UserRole.PROVIDER)


@router.post("", response_model=PatientRead, status_code=status.HTTP_201_CREATED)
async def register_patient(
    body: PatientCreate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*WRITERS)),
) -> PatientRead:
    patient = await service.register_patient(
        session, audit, data=body, actor_user_id=claims.subject
    )
    await session.commit()
    return PatientRead.model_validate(patient)


@router.get("/{patient_id}", response_model=PatientRead)
async def get_patient(
    patient_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*READERS)),
) -> PatientRead:
    patient = await service.get_patient(session, patient_id)
    return PatientRead.model_validate(patient)


@router.get("", response_model=Page[PatientRead])
async def list_patients(
    search: str | None = Query(default=None, max_length=100),
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*READERS)),
) -> Page[PatientRead]:
    patients, total = await service.list_patients(session, page=page, search=search)
    return Page[PatientRead](
        items=[PatientRead.model_validate(p) for p in patients],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.patch("/{patient_id}", response_model=PatientRead)
async def update_patient(
    patient_id: uuid.UUID,
    body: PatientUpdate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(*WRITERS)),
) -> PatientRead:
    patient = await service.update_patient(
        session, audit, patient_id=patient_id, data=body,
        actor_user_id=claims.subject,
    )
    await session.commit()
    return PatientRead.model_validate(patient)
```

The router commits; the service flushes. That keeps the transaction boundary in one place
and lets a service function compose with others inside a single transaction later — which
Week 2's booking workflow will need.

- [ ] **Step 4: Mount it in `app/api/router.py`**

Add the import and the include, keeping the existing two:

```python
from app.modules.patients import router as patients_router
```
```python
api_router.include_router(patients_router.router)
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_patients_authz.py -v`
Expected: 10 passed (two are parametrized)

**No case file in this task** — see Task 4. All three land in Task 9 with their tests.

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/patients/router.py app/api/router.py \
        tests/tiers/t1_contract/test_patients_authz.py
git commit -m "feat: patient management routes with role-based authorisation"
```

---

## Task 7: Provider schemas and service

**Files:**
- Create: `app/modules/providers/schemas.py`, `app/modules/providers/service.py`
- Test: `tests/tiers/t0_unit/test_provider_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t0_unit/test_provider_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from app.modules.providers.schemas import ProviderCreate, ProviderUpdate

pytestmark = pytest.mark.unit


def test_create_requires_identity_and_licence():
    with pytest.raises(ValidationError):
        ProviderCreate(first_name="Ada", last_name="Lovelace")


def test_create_accepts_the_minimum():
    provider = ProviderCreate(
        first_name="Ada", last_name="Lovelace",
        specialty="cardiology", license_number="LIC-1",
    )
    assert provider.is_active is True


def test_create_rejects_a_blank_licence_number():
    with pytest.raises(ValidationError):
        ProviderCreate(
            first_name="Ada", last_name="Lovelace",
            specialty="cardiology", license_number="  ",
        )


def test_update_allows_deactivation():
    update = ProviderUpdate(is_active=False)
    assert update.is_active is False
    assert update.model_dump(exclude_unset=True) == {"is_active": False}


def test_update_rejects_an_empty_body():
    with pytest.raises(ValidationError, match="at least one field"):
        ProviderUpdate()


def test_update_cannot_change_the_licence_number():
    """A licence number identifies the clinician to a regulator; changing it silently
    would break the audit trail's link to the real person."""
    assert "license_number" not in ProviderUpdate.model_fields
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_provider_schemas.py -v`
Expected: collection ERROR — `No module named 'app.modules.providers.schemas'`

- [ ] **Step 3: Create `app/modules/providers/schemas.py`**

```python
"""Provider request and response bodies."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    specialty: str = Field(min_length=1, max_length=120)
    license_number: str = Field(min_length=1, max_length=64)
    is_active: bool = True


class ProviderUpdate(BaseModel):
    """A partial update.

    `license_number` is deliberately absent: it identifies the clinician to a
    regulator, and silently changing it would break the audit trail's link to a real
    person. Correcting one is a deliberate, separate operation.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    specialty: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ProviderUpdate:
        if not self.model_dump(exclude_unset=True):
            raise ValueError("a provider update must set at least one field")
        return self


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    specialty: str
    license_number: str
    is_active: bool
    user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Create `app/modules/providers/service.py`**

```python
"""Provider management.

No FastAPI import — Week 2's Temporal activities call these directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent, AuditLog
from app.core.errors import Conflict, NotFound
from app.core.pagination import PageParams
from app.modules.providers.models import Provider
from app.modules.providers.schemas import ProviderCreate, ProviderUpdate

ENTITY = "provider"


def _snapshot(provider: Provider) -> dict[str, object]:
    return {
        "first_name": provider.first_name,
        "last_name": provider.last_name,
        "specialty": provider.specialty,
        "license_number": provider.license_number,
        "is_active": provider.is_active,
    }


async def register_provider(
    session: AsyncSession,
    audit: AuditLog,
    *,
    data: ProviderCreate,
    actor_user_id: str | None,
) -> Provider:
    provider = Provider(**data.model_dump())
    session.add(provider)
    try:
        await session.flush()
    except IntegrityError as exc:
        if "uq_providers_license_number" in str(exc.orig):
            raise Conflict(
                f"a provider with licence number {data.license_number} already exists"
            ) from exc
        raise

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(provider.id),
            action="registered",
            actor_user_id=actor_user_id,
            after=_snapshot(provider),
        )
    )
    return provider


async def get_provider(session: AsyncSession, provider_id: uuid.UUID) -> Provider:
    provider = await session.get(Provider, provider_id)
    if provider is None:
        raise NotFound(f"provider {provider_id} does not exist")
    return provider


async def list_providers(
    session: AsyncSession,
    *,
    page: PageParams,
    specialty: str | None = None,
    search: str | None = None,
) -> tuple[list[Provider], int]:
    conditions = []
    if specialty:
        conditions.append(Provider.specialty.ilike(specialty))
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(Provider.first_name.ilike(pattern), Provider.last_name.ilike(pattern))
        )

    total = await session.scalar(
        select(func.count()).select_from(Provider).where(*conditions)
    )
    rows = await session.scalars(
        select(Provider)
        .where(*conditions)
        .order_by(Provider.last_name, Provider.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return list(rows), int(total or 0)


async def update_provider(
    session: AsyncSession,
    audit: AuditLog,
    *,
    provider_id: uuid.UUID,
    data: ProviderUpdate,
    actor_user_id: str | None,
) -> Provider:
    provider = await get_provider(session, provider_id)
    before = _snapshot(provider)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    await session.flush()

    await audit.record(
        AuditEvent(
            entity_type=ENTITY,
            entity_id=str(provider.id),
            action="profile_updated",
            actor_user_id=actor_user_id,
            before=before,
            after=_snapshot(provider),
        )
    )
    return provider
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t0_unit/test_provider_schemas.py -v`
Expected: 6 passed

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/providers/schemas.py app/modules/providers/service.py \
        tests/tiers/t0_unit/test_provider_schemas.py
git commit -m "feat: provider schemas and service with an immutable licence number"
```

---

## Task 8: Provider router

**Files:**
- Create: `app/modules/providers/router.py`
- Modify: `app/api/router.py`
- Test: `tests/tiers/t1_contract/test_providers_authz.py`

- [ ] **Step 1: Write the failing authz test**

Create `tests/tiers/t1_contract/test_providers_authz.py`:

```python
from datetime import UTC, datetime

import pytest

from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
from app.settings import Settings

pytestmark = pytest.mark.contract

SETTINGS = Settings(jwt_secret="dev-secret-change-me", jwt_expiry_minutes=30)
NOW = datetime.now(UTC)

VALID_BODY = {
    "first_name": "Ada", "last_name": "Lovelace",
    "specialty": "cardiology", "license_number": "LIC-AUTHZ",
}
SOME_ID = "11111111-1111-1111-1111-111111111111"


def auth(role: UserRole) -> dict[str, str]:
    token = create_access_token(
        subject=SOME_ID, role=role, settings=SETTINGS, now=NOW
    )
    return {"Authorization": f"Bearer {token}"}


async def test_registering_a_provider_requires_a_token(api_client):
    response = await api_client.post("/providers", json=VALID_BODY)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK]
)
async def test_only_an_admin_may_register_a_provider(api_client, role):
    """Front-desk staff register patients, not clinicians."""
    response = await api_client.post("/providers", json=VALID_BODY, headers=auth(role))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "role", [UserRole.PATIENT, UserRole.PROVIDER, UserRole.FRONT_DESK]
)
async def test_only_an_admin_may_update_a_provider(api_client, role):
    response = await api_client.patch(
        f"/providers/{SOME_ID}", json={"specialty": "neurology"}, headers=auth(role)
    )
    assert response.status_code == 403


async def test_listing_providers_requires_only_a_token(api_client):
    """Any authenticated role may look up who they can be seen by."""
    response = await api_client.get("/providers", headers=auth(UserRole.PATIENT))
    assert response.status_code != 403


async def test_listing_providers_rejects_an_anonymous_caller(api_client):
    response = await api_client.get("/providers")
    assert response.status_code == 401


async def test_an_invalid_provider_body_is_rejected(api_client):
    response = await api_client.post(
        "/providers", json={"first_name": "Ada"}, headers=auth(UserRole.ADMIN)
    )
    assert response.status_code == 422


async def test_a_licence_number_cannot_be_patched(api_client):
    """Not in the update schema, so extra keys are ignored rather than applied — this
    asserts the request is accepted for shape but the field is not a valid target."""
    response = await api_client.patch(
        f"/providers/{SOME_ID}",
        json={"license_number": "LIC-NEW"},
        headers=auth(UserRole.ADMIN),
    )
    assert response.status_code == 422
```

The last test relies on `ProviderUpdate` rejecting an empty effective body: with
`license_number` absent from the schema it is ignored, leaving no set fields, which the
`_at_least_one_field` validator rejects as 422. Confirm that is what happens and report if
the behaviour differs.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_providers_authz.py -v`
Expected: failures with 404 — the routes do not exist.

- [ ] **Step 3: Create `app/modules/providers/router.py`**

```python
"""Provider management routes.

Only an admin may create or amend a clinician's record. Any authenticated role may read
one — a patient needs to know who they can be seen by.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_audit_log, page_params
from app.core.audit import AuditLog
from app.core.pagination import Page, PageParams
from app.db.session import get_session
from app.modules.identity.deps import require_role
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims
from app.modules.providers import service
from app.modules.providers.schemas import ProviderCreate, ProviderRead, ProviderUpdate

router = APIRouter(prefix="/providers", tags=["providers"])

ANY_ROLE = tuple(UserRole)


@router.post("", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def register_provider(
    body: ProviderCreate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> ProviderRead:
    provider = await service.register_provider(
        session, audit, data=body, actor_user_id=claims.subject
    )
    await session.commit()
    return ProviderRead.model_validate(provider)


@router.get("/{provider_id}", response_model=ProviderRead)
async def get_provider(
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*ANY_ROLE)),
) -> ProviderRead:
    provider = await service.get_provider(session, provider_id)
    return ProviderRead.model_validate(provider)


@router.get("", response_model=Page[ProviderRead])
async def list_providers(
    specialty: str | None = Query(default=None, max_length=120),
    search: str | None = Query(default=None, max_length=100),
    page: PageParams = Depends(page_params),
    session: AsyncSession = Depends(get_session),
    _: TokenClaims = Depends(require_role(*ANY_ROLE)),
) -> Page[ProviderRead]:
    providers, total = await service.list_providers(
        session, page=page, specialty=specialty, search=search
    )
    return Page[ProviderRead](
        items=[ProviderRead.model_validate(p) for p in providers],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.patch("/{provider_id}", response_model=ProviderRead)
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
    audit: AuditLog = Depends(get_audit_log),
    claims: TokenClaims = Depends(require_role(UserRole.ADMIN)),
) -> ProviderRead:
    provider = await service.update_provider(
        session, audit, provider_id=provider_id, data=body,
        actor_user_id=claims.subject,
    )
    await session.commit()
    return ProviderRead.model_validate(provider)
```

- [ ] **Step 4: Mount it in `app/api/router.py`**

```python
from app.modules.providers import router as providers_router
```
```python
api_router.include_router(providers_router.router)
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t1_contract/test_providers_authz.py -v`
Expected: 13 passed (two are parametrized over three roles)

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/modules/providers/router.py app/api/router.py \
        tests/tiers/t1_contract/test_providers_authz.py
git commit -m "feat: provider management routes, admin-only for writes"
```

---

## Task 9: Integration tests and the case files

This is where the endpoints are proven against a real database and Mongo, and where the
case files from Tasks 4 and 6 get their targets.

**Files:**
- Modify: `tests/tiers/t3_integration/conftest.py` (add three fixtures)
- Create: `tests/tiers/t3_integration/test_auth_api.py`, `tests/tiers/t3_integration/test_patients_api.py`, `tests/tiers/t3_integration/test_providers_api.py`
- Create: `tests/cases/aut-001-login-issues-a-token.yaml`, `tests/cases/pat-001-front-desk-registers-a-patient.yaml`, `tests/cases/prv-001-admin-registers-a-provider.yaml` (whichever Tasks 4 and 6 did not already commit)
- Modify: `tests/cases/CATALOG.md` (regenerated)

- [ ] **Step 1: Add an API client fixture bound to the per-run database**

Append to `tests/tiers/t3_integration/conftest.py`:

```python
@pytest.fixture
async def api(db_settings: Settings, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client against an app pointed at this run's database and Mongo.

    Separate from the T1 `api_client` fixture, which uses default settings and needs no
    infrastructure. This one drives the real stack.
    """
    for key, value in {
        "SMARTHEALTH_POSTGRES_HOST": db_settings.postgres_host,
        "SMARTHEALTH_POSTGRES_PORT": str(db_settings.postgres_port),
        "SMARTHEALTH_POSTGRES_USER": db_settings.postgres_user,
        "SMARTHEALTH_POSTGRES_PASSWORD": db_settings.postgres_password,
        "SMARTHEALTH_POSTGRES_DB": db_settings.postgres_db,
        "SMARTHEALTH_MONGO_HOST": db_settings.mongo_host,
        "SMARTHEALTH_MONGO_PORT": str(db_settings.mongo_port),
        "SMARTHEALTH_MONGO_DB": db_settings.mongo_db,
        "SMARTHEALTH_REDIS_HOST": db_settings.redis_host,
        "SMARTHEALTH_REDIS_PORT": str(db_settings.redis_port),
        "SMARTHEALTH_REDIS_DB": str(db_settings.redis_db),
    }.items():
        monkeypatch.setenv(key, value)

    async with LifespanManager(create_app()) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


@pytest.fixture
def token_for(db_settings: Settings):
    """Mint a token the running app will accept, for any role."""

    def _mint(role: UserRole, subject: str | None = None) -> dict[str, str]:
        token = create_access_token(
            subject=subject or str(uuid.uuid4()),
            role=role,
            settings=db_settings,
            now=datetime.now(UTC),
        )
        return {"Authorization": f"Bearer {token}"}

    return _mint


@pytest.fixture
async def audit_documents(db_settings: Settings):
    """Read and clear the audit collection for this run."""
    client = create_mongo_client(db_settings)
    collection = get_audit_collection(client, db_settings)
    await collection.delete_many({})
    try:
        yield collection
    finally:
        await collection.delete_many({})
        await client.close()
```

Add to that file's import block:

```python
import uuid
from datetime import UTC, datetime

import httpx
from asgi_lifespan import LifespanManager

from app.db.mongo import create_mongo_client, get_audit_collection
from app.main import create_app
from app.modules.identity.models import UserRole
from app.modules.identity.security import create_access_token
```


- [ ] **Step 2: Create `tests/tiers/t3_integration/test_auth_api.py`**

```python
import uuid

import pytest

from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def _user(session, *, email: str, password: str, active: bool = True) -> User:
    user = User(
        email=email, password_hash=hash_password(password),
        role=UserRole.ADMIN, is_active=active,
    )
    session.add(user)
    await session.commit()
    return user


async def test_login_issues_a_usable_token(api, db_session):
    """Referenced by tests/cases/aut-001-login-issues-a-token.yaml."""
    email = f"admin-{uuid.uuid4().hex[:8]}@example.test"
    await _user(db_session, email=email, password="correct horse battery staple")

    response = await api.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    # The token must actually open a protected route.
    listing = await api.get(
        "/providers", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert listing.status_code == 200


async def test_a_wrong_password_and_an_unknown_email_are_indistinguishable(api, db_session):
    """Different messages would tell an attacker which addresses are registered."""
    email = f"admin-{uuid.uuid4().hex[:8]}@example.test"
    await _user(db_session, email=email, password="right")

    wrong_password = await api.post(
        "/auth/login", json={"email": email, "password": "wrong"}
    )
    unknown_email = await api.post(
        "/auth/login",
        json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.test", "password": "x"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_an_inactive_account_cannot_log_in(api, db_session):
    email = f"admin-{uuid.uuid4().hex[:8]}@example.test"
    await _user(db_session, email=email, password="right", active=False)

    response = await api.post(
        "/auth/login", json={"email": email, "password": "right"}
    )
    assert response.status_code == 401
```

- [ ] **Step 3: Create `tests/tiers/t3_integration/test_patients_api.py`**

```python
import uuid

import pytest

from app.modules.identity.models import UserRole

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _body(**overrides) -> dict:
    body = {
        "mrn": f"MRN-{uuid.uuid4().hex[:8]}",
        "first_name": "Jo",
        "last_name": "Bloggs",
    }
    body.update(overrides)
    return body


async def test_front_desk_registers_a_patient(api, token_for, audit_documents):
    """Referenced by tests/cases/pat-001-front-desk-registers-a-patient.yaml.

    A walk-in has no user account — the requirement that made user_id nullable.
    """
    response = await api.post(
        "/patients", json=_body(), headers=token_for(UserRole.FRONT_DESK)
    )
    assert response.status_code == 201
    created = response.json()
    assert created["user_id"] is None
    assert created["id"]

    stored = await audit_documents.find_one({"entity_id": created["id"]})
    assert stored is not None, "registering a patient wrote no audit document"
    assert stored["action"] == "registered"
    assert stored["entity_type"] == "patient"
    assert stored["before"] is None
    assert stored["after"]["mrn"] == created["mrn"]


async def test_a_duplicate_mrn_is_a_conflict(api, token_for):
    body = _body()
    headers = token_for(UserRole.ADMIN)
    first = await api.post("/patients", json=body, headers=headers)
    assert first.status_code == 201

    second = await api.post("/patients", json=body, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"] == "Conflict"
    assert body["mrn"] in second.json()["detail"]


async def test_reading_an_absent_patient_is_a_404(api, token_for):
    response = await api.get(
        f"/patients/{uuid.uuid4()}", headers=token_for(UserRole.ADMIN)
    )
    assert response.status_code == 404
    assert response.json()["error"] == "NotFound"


async def test_an_update_is_audited_with_before_and_after(api, token_for, audit_documents):
    headers = token_for(UserRole.FRONT_DESK)
    created = (await api.post("/patients", json=_body(), headers=headers)).json()

    response = await api.patch(
        f"/patients/{created['id']}",
        json={"phone": "+44 20 7946 0000"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["phone"] == "+44 20 7946 0000"

    updates = await audit_documents.find(
        {"entity_id": created["id"], "action": "profile_updated"}
    ).to_list(length=10)
    assert len(updates) == 1
    assert updates[0]["before"]["phone"] is None
    assert updates[0]["after"]["phone"] == "+44 20 7946 0000"


async def test_a_partial_update_does_not_erase_unsent_fields(api, token_for):
    """A PATCH that nulled every omitted field would silently destroy data."""
    headers = token_for(UserRole.ADMIN)
    created = (
        await api.post("/patients", json=_body(phone="+1 555 0100"), headers=headers)
    ).json()

    updated = (
        await api.patch(
            f"/patients/{created['id']}", json={"first_name": "Josephine"},
            headers=headers,
        )
    ).json()

    assert updated["first_name"] == "Josephine"
    assert updated["phone"] == "+1 555 0100"


async def test_search_matches_mrn_and_name(api, token_for):
    headers = token_for(UserRole.ADMIN)
    unique = uuid.uuid4().hex[:8]
    await api.post(
        "/patients", json=_body(mrn=f"MRN-{unique}", last_name=f"Zeta{unique}"),
        headers=headers,
    )

    by_mrn = (await api.get(f"/patients?search={unique}", headers=headers)).json()
    assert by_mrn["total"] >= 1

    by_name = (await api.get(f"/patients?search=Zeta{unique}", headers=headers)).json()
    assert by_name["total"] == 1


async def test_pagination_reports_the_total_and_slices(api, token_for):
    headers = token_for(UserRole.ADMIN)
    marker = uuid.uuid4().hex[:8]
    for index in range(3):
        await api.post(
            "/patients",
            json=_body(mrn=f"MRN-{marker}-{index}", last_name=f"Page{marker}"),
            headers=headers,
        )

    page = (
        await api.get(f"/patients?search=Page{marker}&limit=2&offset=0", headers=headers)
    ).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2

    second = (
        await api.get(f"/patients?search=Page{marker}&limit=2&offset=2", headers=headers)
    ).json()
    assert len(second["items"]) == 1
```

- [ ] **Step 4: Create `tests/tiers/t3_integration/test_providers_api.py`**

```python
import uuid

import pytest

from app.modules.identity.models import UserRole

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def _body(**overrides) -> dict:
    body = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "specialty": "cardiology",
        "license_number": f"LIC-{uuid.uuid4().hex[:10]}",
    }
    body.update(overrides)
    return body


async def test_admin_registers_a_provider(api, token_for, audit_documents):
    """Referenced by tests/cases/prv-001-admin-registers-a-provider.yaml."""
    response = await api.post(
        "/providers", json=_body(), headers=token_for(UserRole.ADMIN)
    )
    assert response.status_code == 201
    created = response.json()
    assert created["is_active"] is True

    stored = await audit_documents.find_one({"entity_id": created["id"]})
    assert stored is not None, "registering a provider wrote no audit document"
    assert stored["entity_type"] == "provider"
    assert stored["after"]["specialty"] == "cardiology"


async def test_a_duplicate_licence_number_is_a_conflict(api, token_for):
    body = _body()
    headers = token_for(UserRole.ADMIN)
    assert (await api.post("/providers", json=body, headers=headers)).status_code == 201

    second = await api.post("/providers", json=body, headers=headers)
    assert second.status_code == 409
    assert body["license_number"] in second.json()["detail"]


async def test_filtering_by_specialty(api, token_for):
    headers = token_for(UserRole.ADMIN)
    marker = uuid.uuid4().hex[:8]
    await api.post("/providers", json=_body(specialty=f"cardio{marker}"), headers=headers)
    await api.post("/providers", json=_body(specialty=f"neuro{marker}"), headers=headers)

    page = (
        await api.get(f"/providers?specialty=cardio{marker}", headers=headers)
    ).json()
    assert page["total"] == 1
    assert page["items"][0]["specialty"] == f"cardio{marker}"


async def test_a_patient_may_read_providers_but_not_create_them(api, token_for):
    """A patient needs to know who they can be seen by."""
    patient_headers = token_for(UserRole.PATIENT)
    assert (await api.get("/providers", headers=patient_headers)).status_code == 200
    assert (
        await api.post("/providers", json=_body(), headers=patient_headers)
    ).status_code == 403


async def test_deactivating_a_provider_is_audited(api, token_for, audit_documents):
    headers = token_for(UserRole.ADMIN)
    created = (await api.post("/providers", json=_body(), headers=headers)).json()

    updated = (
        await api.patch(
            f"/providers/{created['id']}", json={"is_active": False}, headers=headers
        )
    ).json()
    assert updated["is_active"] is False

    events = await audit_documents.find(
        {"entity_id": created["id"], "action": "profile_updated"}
    ).to_list(length=10)
    assert len(events) == 1
    assert events[0]["before"]["is_active"] is True
    assert events[0]["after"]["is_active"] is False
```

- [ ] **Step 5: Run the integration suite**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t3_integration -v`
Expected: the 21 existing plus 3 auth + 7 patient + 5 provider = 36 passed. Report the
actual number.

**If an audit assertion fails because no document was written**, that is a real defect in
the service — report it rather than relaxing the assertion. The whole point of writing
audit events awaited was that a missing one fails loudly.

- [ ] **Step 6: Create the three case files**

`tests/cases/aut-001-login-issues-a-token.yaml`:

```yaml
id: aut-001-login-issues-a-token
title: "A valid email and password exchange for a bearer token"
requirement: [PART-A-FR-1]
tier: integration
priority: P0
status: ready
impl: "tests/tiers/t3_integration/test_auth_api.py::test_login_issues_a_usable_token"
```

`tests/cases/pat-001-front-desk-registers-a-patient.yaml`:

```yaml
id: pat-001-front-desk-registers-a-patient
title: "Front-desk staff register a walk-in patient with no user account"
requirement: [PART-A-FR-1]
tier: integration
priority: P0
status: ready
impl: "tests/tiers/t3_integration/test_patients_api.py::test_front_desk_registers_a_patient"
```

`tests/cases/prv-001-admin-registers-a-provider.yaml`:

```yaml
id: prv-001-admin-registers-a-provider
title: "An admin registers a provider and the change is audited"
requirement: [PART-A-FR-1]
tier: integration
priority: P0
status: ready
impl: "tests/tiers/t3_integration/test_providers_api.py::test_admin_registers_a_provider"
```

All three are created here; Tasks 4 and 6 deliberately created none.

- [ ] **Step 7: Regenerate the catalog and verify**

```bash
.venv/Scripts/python.exe -m tests.runner.route_check --write-catalog --write-traceability
.venv/Scripts/python.exe -m pytest tests/tiers/test_catalog.py -v
```
Expected: `route_check` exits 0 with 5 cases; every `test_impl_pointer_resolves`
parametrization passes. A committed `CATALOG.md` that drifts fails its own test, so the
regeneration is required.

- [ ] **Step 8: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m pytest -m docker -q
.venv/Scripts/python.exe -m ruff check app tests
git add tests/tiers/t3_integration tests/cases
git commit -m "test: prove the management endpoints against real infrastructure"
```

---

## Task 10: `create-user` CLI

**Files:**
- Create: `app/cli.py`
- Test: `tests/tiers/t3_integration/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tiers/t3_integration/test_cli.py`:

```python
import uuid

import pytest
from sqlalchemy import select

from app.cli import create_user
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import verify_password

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_create_user_inserts_a_usable_account(db_settings, db_session):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.test"
    await create_user(
        settings=db_settings, email=email, password="a strong secret", role="admin"
    )

    user = await db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.role is UserRole.ADMIN
    assert user.is_active is True
    assert verify_password("a strong secret", user.password_hash)


async def test_create_user_refuses_a_duplicate_email(db_settings):
    email = f"cli-{uuid.uuid4().hex[:8]}@example.test"
    await create_user(settings=db_settings, email=email, password="x1", role="admin")

    with pytest.raises(SystemExit) as exit_info:
        await create_user(settings=db_settings, email=email, password="x2", role="admin")
    assert exit_info.value.code == 1


async def test_create_user_rejects_an_unknown_role(db_settings):
    with pytest.raises(SystemExit):
        await create_user(
            settings=db_settings,
            email=f"cli-{uuid.uuid4().hex[:8]}@example.test",
            password="x",
            role="superuser",
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t3_integration/test_cli.py -v`
Expected: collection ERROR — `No module named 'app.cli'`

- [ ] **Step 3: Create `app/cli.py`**

```python
"""Administrative commands.

`create-user` exists so a running system is demonstrable: without it, login can only be
exercised by the test suite, and a reviewer has no way in.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.settings import Settings, get_settings


async def create_user(*, settings: Settings, email: str, password: str, role: str) -> None:
    """Insert one user. Exits non-zero rather than raising a traceback at a human."""
    try:
        parsed_role = UserRole(role)
    except ValueError:
        valid = ", ".join(member.value for member in UserRole)
        print(f"unknown role '{role}'; expected one of: {valid}", file=sys.stderr)
        raise SystemExit(1) from None

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            existing = await session.scalar(select(User).where(User.email == email))
            if existing is not None:
                print(f"a user with email {email} already exists", file=sys.stderr)
                raise SystemExit(1)

            session.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    role=parsed_role,
                )
            )
            await session.commit()
            print(f"created {parsed_role.value} {email}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create-user", help="insert one user account")
    create.add_argument("--email", required=True)
    create.add_argument("--password", required=True)
    create.add_argument(
        "--role",
        required=True,
        choices=[member.value for member in UserRole],
    )

    args = parser.parse_args(argv)
    if args.command == "create-user":
        asyncio.run(
            create_user(
                settings=get_settings(),
                email=args.email,
                password=args.password,
                role=args.role,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    main()
```

Note the double guard on role: `argparse` `choices` catches it at the command line, and
`UserRole(role)` catches it when `create_user` is called directly — which is how the test
reaches the `SystemExit`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/tiers/t3_integration/test_cli.py -v`
Expected: 3 passed

- [ ] **Step 5: Exercise it as a human would**

Against the running stack, create an admin and then log in with it via `curl` or `httpx`:

```bash
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth \
  .venv/Scripts/python.exe -m app.cli create-user \
  --email demo@example.test --password "demo password" --role admin
```

Then start the app (`uvicorn app.main:app --port 8001` with the same env) and
`POST /auth/login`. Confirm you receive a token and that it opens `GET /providers`.
**Paste the token response** (redacting the token itself) and the providers status code.
Then delete the demo user.

- [ ] **Step 6: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -q
.venv/Scripts/python.exe -m pytest -m docker -q
.venv/Scripts/python.exe -m ruff check app tests
git add app/cli.py tests/tiers/t3_integration/test_cli.py
git commit -m "feat: create-user command so a running system is demonstrable"
```

---

## Task 11: Dockerfile and the app service

**Files:**
- Create: `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore`
- Modify: `docker-compose.infra.yml`

- [ ] **Step 1: Create `.dockerignore`**

```
.git
.venv
__pycache__
*.pyc
.pytest_cache
.ruff_cache
tests/reports
docs
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
# Single-stage: the dependency set is small and the image is for local demonstration,
# not a size-sensitive deployment.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies before copying the source, so a code change does not
# invalidate the dependency layer.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Run as a non-root user: a container that does not need root should not have it.
RUN useradd --create-home --uid 10001 smarthealth && chown -R smarthealth /app
USER smarthealth

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
```

`pip install .` (not `-e .`) because the container has no reason to install editable, and
`[tool.setuptools.packages.find] include = ["app*"]` means only the application package
ships.

- [ ] **Step 3: Create `docker-entrypoint.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Migrating here is right for one replica and wrong for several, which would race.
# Alembic takes a lock so the race is safe rather than corrupting, but replicas would
# serialise on startup. In production this becomes a separate job.
echo "applying migrations..."
python -m alembic upgrade head

echo "starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 4: Add the `app` service to `docker-compose.infra.yml`**

Append to the `services:` block, before `volumes:`:

```yaml
  app:
    # Behind a profile so the test stack, which runs this same file under
    # -p smarthealth-test, does not try to start an app container on every
    # `up -d --wait`. The tests drive the application in-process.
    profiles: ["app"]
    build: .
    environment:
      SMARTHEALTH_ENVIRONMENT: docker
      SMARTHEALTH_POSTGRES_HOST: postgres
      SMARTHEALTH_POSTGRES_PORT: 5432
      SMARTHEALTH_POSTGRES_USER: ${POSTGRES_USER:-smarthealth}
      SMARTHEALTH_POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-smarthealth}
      SMARTHEALTH_POSTGRES_DB: ${POSTGRES_DB:-smarthealth}
      SMARTHEALTH_MONGO_HOST: mongo
      SMARTHEALTH_MONGO_PORT: 27017
      SMARTHEALTH_MONGO_DB: ${POSTGRES_DB:-smarthealth}
      SMARTHEALTH_REDIS_HOST: redis
      SMARTHEALTH_REDIS_PORT: 6379
      SMARTHEALTH_JWT_SECRET: ${SMARTHEALTH_JWT_SECRET:-dev-secret-change-me}
    ports: ["${APP_PORT:-8000}:8000"]
    depends_on:
      postgres:
        condition: service_healthy
      mongo:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 10s
      timeout: 5s
      retries: 12
```

Note the container talks to `postgres:5432` — the **internal** port. The 15432 offset is
only a host-side publication.

- [ ] **Step 5: Prove the test stack is unaffected**

```bash
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml config --quiet
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml up -d --wait
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml ps --format json | \
  .venv/Scripts/python.exe -c "import json,sys; print(sorted(json.loads(l)['Service'] for l in sys.stdin if l.strip()))"
```
Expected: the same seven services, **no `app`** — proving the profile keeps it out.
Then `.venv/Scripts/python.exe -m pytest -m docker -q` still passes.

- [ ] **Step 6: Build and run the full system**

```bash
docker compose --profile app -f docker-compose.infra.yml build app
docker compose --profile app -f docker-compose.infra.yml up -d --wait
```

Deliberately **no `-p` and no `--env-file`**: that gives project `smarthealth` with the
compose file's default ports (5432/27017/6379), which are free. Reusing `.env.test` would
try to publish 15432 again and collide with the running test stack, since two stacks
cannot bind the same host port.

Then confirm from the host: `curl http://localhost:8000/health` returns `{"status":"ok",...}`
and `curl http://localhost:8000/ready` returns 200 with all three dependencies `ok`.
**Paste both.** Also confirm the container applied migrations — check its logs for
`applying migrations` and confirm `alembic_version` holds `0003` in that stack's database.

- [ ] **Step 7: Tear down only the app stack**

```bash
docker compose --profile app -f docker-compose.infra.yml down
```
Confirm the `smarthealth-test` stack is still healthy afterwards.

- [ ] **Step 8: Commit**

```bash
git add Dockerfile docker-entrypoint.sh .dockerignore docker-compose.infra.yml
git commit -m "feat: containerise the app behind a compose profile"
```

---

## Task 12: Documentation and final verification

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `tests/README.md`

- [ ] **Step 1: Add the API surface to `CLAUDE.md`**

Append a new section after `## Domain model`:

```markdown
## API surface

| Method | Path | Roles |
| --- | --- | --- |
| POST | `/auth/login` | public |
| POST | `/patients` | `front_desk`, `admin` |
| GET | `/patients/{id}`, `GET /patients` | `front_desk`, `admin`, `provider` |
| PATCH | `/patients/{id}` | `front_desk`, `admin` |
| POST | `/providers`, `PATCH /providers/{id}` | `admin` |
| GET | `/providers/{id}`, `GET /providers` | any authenticated |

`GET /health` and `GET /ready` are public.

**Layering:** `router → service → SQLAlchemy`. Services never import FastAPI — they raise
domain errors from `app/core/errors.py` that exception handlers translate, so Week 2's
Temporal activities can call the same functions. The router owns the transaction boundary
(`session.commit()`); services flush.

**Every mutation is audited** to Mongo with actor, action, and before/after, awaited so a
Mongo outage fails the request rather than silently dropping the record.

**Not yet implemented:** patients reading their own record. That needs per-object
authorisation rather than per-role, and the requirements group patient self-service with
booking (Week 2).

### Running it

```bash
# an account to log in with
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth \
  python -m app.cli create-user --email you@example.test --password "..." --role admin

# the whole system in containers
docker compose --profile app -f docker-compose.infra.yml up -d --wait
```
```

- [ ] **Step 2: Add the containerised run to `README.md`**

In the Setup section, after the existing compose line, add:

```markdown
To run the application itself in a container alongside the infrastructure:

```bash
docker compose --profile app -f docker-compose.infra.yml up -d --wait
curl http://localhost:8000/health
```

The `app` service sits behind a compose profile so the test stack, which uses the same
file, does not start it — the tests drive the application in-process.
```

- [ ] **Step 3: Note the new tier split in `tests/README.md`**

In the Tiers table, change the `contract` row's "Proves" cell to
"API surface, authorisation matrix, request validation, schema compatibility".

- [ ] **Step 4: Full verification**

Run each and report:

```bash
.venv/Scripts/python.exe -m pytest -m "not docker" -v
.venv/Scripts/python.exe -m pytest -m docker -v
.venv/Scripts/python.exe -m pytest -v
.venv/Scripts/python.exe -m ruff check app tests
.venv/Scripts/python.exe -m tests.runner.route_check --write-catalog --write-traceability
git status --short
```

- [ ] **Step 5: Verify every documented command in a clean shell**

This repo has twice reported documentation as working because an inherited environment
variable made it work. Do not repeat that:

```bash
env -u SMARTHEALTH_POSTGRES_PORT -u SMARTHEALTH_POSTGRES_DB -u SMARTHEALTH_POSTGRES_HOST \
  bash -c 'cd /c/Work/Upskill/SmartHealth && source .venv/Scripts/activate && <command>'
```

Run every command in `CLAUDE.md`, `README.md` and `tests/README.md` that way, confirm
`env | grep -c SMARTHEALTH` reports 0 first, and report any that fails.

- [ ] **Step 6: Confirm the Stop hook still behaves**

```bash
python .claude/hooks/feature_test_stop.py; echo "exit=$?"
```
Expected 0 on a clean tree. Then touch `app/modules/patients/service.py`, confirm exit 2,
and restore.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md README.md tests/README.md tests/cases/CATALOG.md
git commit -m "docs: record the API surface, layering, and containerised run"
```

---

## Definition of done

- [ ] Both lanes pass with zero collection errors
- [ ] `POST /auth/login` returns a token that opens a protected route
- [ ] An unknown email and a wrong password are indistinguishable in status and body
- [ ] An inactive account cannot log in
- [ ] Patient register/read/list/update work, with 409 on duplicate MRN and 404 on absent
- [ ] A partial update does not erase unsent fields
- [ ] Provider register/read/list/update work, with 409 on duplicate licence number
- [ ] `license_number` cannot be changed through `PATCH`
- [ ] The authorisation matrix is enforced for every endpoint, tested at T1 without containers
- [ ] Pagination is bounded at 200 and rejects out-of-range limits with 422
- [ ] **Every mutation writes an audit document with before/after**, proven at T3
- [ ] `python -m app.cli create-user` inserts a usable account and refuses duplicates
- [ ] `docker compose --profile app up` runs the whole system; `/health` and `/ready` answer
- [ ] The test stack still starts seven services, without `app`
- [ ] Five case files, all with resolvable `impl` pointers, and `CATALOG.md` current
- [ ] Every documented command verified in a shell with no inherited `SMARTHEALTH_*`
