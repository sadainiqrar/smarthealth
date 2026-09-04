# Week 1 Completion — Management APIs — Design

**Date:** 2026-09-03
**Status:** Approved (design); implementation plan pending
**Depends on:** `2026-09-02-week1-foundation-design.md` (schema, auth skeleton, lifespan, test tiers)

---

## 1. Scope

The endpoints Week 1 names as deliverables and the foundation slice deliberately deferred:
*"patient and provider management APIs"*, *"user roles and auth basics"*, and
*"Docker setup operational"*.

**In scope:** `POST /auth/login`; patient register, read, list, update; provider register,
read, list, update; audit writes on every mutation; a `create-user` CLI; a Dockerfile and
an `app` service in the compose topology; tests at T1 and T3; a case file per endpoint
group.

**Out of scope:** appointments and booking (Week 2), clinic and department management
endpoints (seeded directly until something needs to manage them), Kafka and Celery
(Week 3), and patient self-service access to their own record (see §5).

## 2. Request path

```
router.py     HTTP shape, authz via require_role, status codes
    ↓
service.py    business rules, audit emission, transaction boundary
    ↓
SQLAlchemy    AsyncSession from the request dependency
```

**Services never import FastAPI.** This is the whole justification for the layer: Week 2's
Temporal activities call the same functions without an HTTP request in sight, and an
activity that catches `HTTPException` would be nonsense. Services raise domain errors;
routers and exception handlers translate.

No repository layer. SQLAlchemy already is one, and a class whose methods forward single
calls to `session.execute` adds indirection without adding a seam.

## 3. Cross-cutting modules

### 3.1 `app/core/errors.py`

```python
class DomainError(Exception)
class NotFound(DomainError)          # → 404
class Conflict(DomainError)          # → 409
class PermissionDenied(DomainError)  # → 403
```

Each carries a human-readable message. `create_app` registers one handler per type,
returning a consistent body: `{"error": "<type>", "detail": "<message>"}`.

The `require_role` dependency keeps raising `HTTPException` directly — it is HTTP-layer
machinery, not domain logic, and 401 has no domain meaning.

### 3.2 `app/core/audit.py`

Mongo owns the audit trail (foundation spec §3.4). This module is how writes reach it.

```python
@dataclass(frozen=True)
class AuditEvent:
    entity_type: str
    entity_id: str
    action: str
    actor_user_id: str | None
    before: dict | None
    after: dict | None

class AuditLog:
    def __init__(self, collection, clock: Clock) -> None
    async def record(self, event: AuditEvent) -> None
```

`get_audit_log(request)` builds one from `request.app.state.mongo`. Services receive an
`AuditLog` as a parameter, so they never learn where the collection came from and can be
handed a fake in tests.

**Awaited, not fire-and-forget.** A dropped audit entry is invisible and unrecoverable; a
failed request is neither. If Mongo is down, the write fails loudly.

### 3.3 `app/core/pagination.py`

`limit` (default 50, maximum 200) and `offset` query parameters, and a generic
`Page[T]` response carrying `items`, `total`, `limit`, `offset`. The maximum exists so a
single request cannot ask the database for every patient in the system.

## 4. Endpoints

| Method | Path | Roles | Notes |
|---|---|---|---|
| POST | `/auth/login` | public | email + password → access token |
| POST | `/patients` | `front_desk`, `admin` | 409 on duplicate MRN |
| GET | `/patients/{id}` | `front_desk`, `admin`, `provider` | 404 if absent |
| GET | `/patients` | `front_desk`, `admin`, `provider` | search by name or MRN, paginated |
| PATCH | `/patients/{id}` | `front_desk`, `admin` | partial update, audited |
| POST | `/providers` | `admin` | 409 on duplicate licence number |
| GET | `/providers/{id}` | any authenticated | |
| GET | `/providers` | any authenticated | filter by specialty, paginated |
| PATCH | `/providers/{id}` | `admin` | partial update, audited |

Front-desk staff register patients, which is why `POST /patients` is not admin-only — it
is the same requirement that made `patients.user_id` nullable in the foundation slice.

### 4.1 Login

`POST /auth/login` takes email and password, looks up the user, verifies the hash, and
returns `{access_token, token_type: "bearer", expires_in}`.

A failed login returns **401 with the same message whether the email is unknown or the
password is wrong**. Distinguishing them tells an attacker which addresses are registered.
`verify_password` already treats an unparseable stored hash as a failed login rather than
raising, so a corrupt row cannot turn into a 500 on this path.

## 5. Deliberately deferred: patient self-access

A patient reading their own record needs a `user_id → patient` lookup and a per-object
rule rather than a per-role one — a different shape of authorisation from everything else
here. The requirements place patient self-service ("access booking history") alongside
booking, so it belongs with Week 2 rather than half-built now.

`require_role` stays purely role-based in this slice. Recording the gap so it is a
decision rather than an oversight.

## 6. Getting the first user in

Login is unusable by a human reviewer if no user can exist, and seeding one through tests
only proves it to the test suite.

`python -m app.cli create-user --email … --password … --role admin` hashes the password
and inserts the row, refusing politely if the email is taken. One command, and the running
system becomes demonstrable rather than merely tested.

## 7. Docker

A `Dockerfile` (python:3.13-slim, non-root user, dependencies installed from
`pyproject.toml`) and an `app` service in `docker-compose.infra.yml` that depends on
Postgres reporting healthy. The entrypoint runs `alembic upgrade head`, then `uvicorn`.

**The `app` service sits behind a compose profile** (`profiles: ["app"]`), so it starts
only for `docker compose --profile app up`. Without that, the test stack — which runs the
same file under `-p smarthealth-test` — would try to start an app container on every
`up -d --wait`, fail for want of a built image, and break the integration lane. The tests
drive the application in-process; they need the infrastructure, not a container of it.

**Named tradeoff:** migrating in the entrypoint is right for one replica and wrong for
several, which would race each other. Alembic takes a lock so the race is safe rather than
corrupting, but replicas would still serialise on startup. In production this becomes a
separate migration job; for a single-container assignment it is the simplest thing that
works, and recording the limit here means it is a decision rather than an omission.

## 8. Testing

| Tier | What it covers |
|---|---|
| **T1 contract** | Authz for every endpoint (each role against each route), request validation, response shape, pagination bounds — with the service dependency overridden by a fake. No containers. |
| **T3 integration** | Real behaviour against the per-run database: duplicate MRN returns 409, search and pagination return the right rows, and **audit documents actually land in Mongo** with the right before/after. |

The split matters: T1 proves the HTTP contract without paying for a database, T3 proves
the behaviour that only a real database can demonstrate. Putting authz tests at T3 would
make the fast lane blind to the most security-relevant logic in the slice.

**Every endpoint group also gets a case file** in `tests/cases/`. The `Stop` hook is armed
for `app/**`, so a task that adds an endpoint without a case cannot finish — the harness
gating its own construction.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Audit writes make every mutation depend on Mongo | Accepted deliberately (§3.2). `/ready` already reports Mongo separately, so an outage is diagnosable rather than mysterious. |
| Service functions grow into a dumping ground | One service module per domain, each function taking its dependencies explicitly. If a module outgrows comprehension, it splits by use case, not by layer. |
| T1 fakes drift from the real services | The same behaviours are asserted at T3 against the real database; a fake that lies produces a passing T1 and a failing T3, not silent agreement. |
| Entrypoint migrations under multiple replicas | Documented in §7; becomes a separate job when there is more than one replica. |

## 10. Open questions

None blocking. One to revisit in Week 2: whether `AuditLog` should batch writes once
booking starts emitting several events per workflow. Deferred until there is a measured
reason — batching an audit trail trades durability for throughput, and that trade needs
evidence.

## 11. Deliberately deferred

Written down so each is a decision a reviewer can disagree with, rather than something
nobody noticed. None of them is a bug in what shipped; all of them are things a reader of
the code could reasonably expect to find and will not.

**A deactivated user keeps access until their token expires.** `decode_access_token`
validates the signature and `exp` and nothing else; `is_active` is read once, at login.
So revoking an account stops the next login but not the token already in the attacker's
hands, for up to `jwt_expiry_minutes`. Closing the gap means a database lookup of the
user on every request, which trades away exactly the statelessness the JWT was chosen
for. The honest fix is a short expiry plus a deny-list in Redis when there is a reason to
build one — not a silent per-request query. Sixty minutes is the current exposure window.

**A patient cannot read their own record.** Covered in §5: `require_role` is purely
role-based, and self-access needs authorisation against the object, not the role. The
requirements group patient self-service with booking, so it lands in Week 2 alongside the
first endpoint that genuinely needs per-object rules.

**`list_providers` has no `is_active` filter**, so a deactivated clinician still appears
in listings. That is wrong for a "who can I book with" screen and right for an admin
roster, and Week 1 has only the roster. Adding the filter later is additive as long as
the default stays unfiltered; making it default to active-only later would be a silent
behaviour change to an endpoint someone had already built against, which is why it is not
being guessed at now.

**Deactivating a provider ignores their future appointments.** There are no appointments
yet, so nothing is broken today — but the moment scheduling exists, a deactivation has to
decide what happens to booked slots: cancel and notify, reassign, or refuse the
deactivation. That is a multi-step operation across scheduling, notification and billing
that must not half-apply, which is precisely the shape Temporal is in the stack for. It
belongs in Week 2, as a workflow, not as an extra `UPDATE` bolted onto the provider
service.

**Specialty filtering is a sequential scan.** The filter compiles to
`func.lower(providers.specialty) = lower(:value)`, and a plain B-tree index on
`specialty` cannot serve a call over the column — `ix_providers_specialty` is not used.
At Week 1 row counts this is unmeasurable, so paying for the fix now would be
speculative. When it matters the answer is a functional index on `lower(specialty)` or
moving the column to `citext`; the latter is tidier at the call site but adds an
extension to the migration path, so it deserves a deliberate choice rather than a default.

**The container migrates in its entrypoint.** Covered in §7. Right at one replica,
racy at several — Alembic's lock makes the race safe rather than corrupting, but replicas
serialise on startup. It becomes a separate migration job the first time there is more
than one replica.

**The compose healthcheck polls `/health`, not `/ready`.** `/health` answers "is the
process alive"; `/ready` answers "can it serve", and only the second notices a dependency
that dies after startup. The healthcheck therefore keeps reporting the container healthy
while every request fails on a downed Postgres. It is deliberate for now: `--wait` uses
the healthcheck as a startup gate, and a readiness-based gate would make the app's own
startup depend on dependencies that `depends_on: service_healthy` has already gated. The
same retry budget (12 x 10s) doubles as the timeout for the entrypoint's
`alembic upgrade head`, which is worth knowing before shrinking it — a slow migration
would then be reported as an unhealthy container.

**There is no dependency lock file.** `pyproject.toml` carries ranges, so `pip install`
resolves whatever is current, and the image rebuilt in six months may not be the image
built today. For an assignment graded on architecture that is an acceptable trade against
the churn of maintaining a lock; for anything deployed it is not. The fix is a compiled
requirements file (pip-compile / uv) referenced by the Dockerfile.
