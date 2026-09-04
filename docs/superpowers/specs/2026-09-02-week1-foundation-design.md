# Week 1 Foundation — Design

**Date:** 2026-09-02
**Status:** Approved (design); implementation plan pending
**Depends on:** `2026-09-01-testing-harness-design.md` (the harness is already built and green)

---

## 1. Scope

The codebase, database, and tooling foundation for Part A. Everything an endpoint would need,
without the endpoints.

**In scope:** module structure; async SQLAlchemy + Alembic; the domain schema for identity,
patients, providers, clinics, scheduling, and visits; Mongo and Redis clients; an auth
skeleton (users, hashing, JWT helpers, a `require_role` dependency); core utilities the
harness mandates (injectable clock, registry, structured logging); liveness and readiness
endpoints; tests at T0, T1, and T3.

**Out of scope:** HTTP endpoints for patients/providers/appointments, business logic,
services and repositories, Temporal workflows, Kafka, Celery. Those are Weeks 1b–3.

## 2. Non-goals

- No `repository.py` / `service.py` / `router.py` placeholder files. Empty modules are noise;
  each arrives with the endpoint that needs it.
- No `billing_records` table. Its shape follows the billing pre-check activity (Week 2);
  guessing now guarantees a rewrite.
- No Kafka `outbox` table. It lands with the producer in Week 3.
- No `Dockerfile` or app service in compose. The infrastructure topology already exists and
  runs; containerising the app is deferred until there is an app worth serving.
- No audit *writes*. There are no profile changes to audit yet — the collection, its indexes,
  and a round-trip test land now; the write path lands with the services.

## 3. Requirement ambiguities, and how they are resolved

The requirements documents leave five questions unanswered that a first migration cannot
avoid. Each resolution below is a decision, not a reading — recorded here so it can be
challenged later.

### 3.1 A patient is not a user

`part-a-core-platform.md` lists `patient` among the four user roles, but also has front-desk
staff performing "patient registration". Both cannot be true of one entity: a walk-in
registered at the desk has no credentials.

**Decision.** `users` holds authentication identity only — email, password hash, role,
active flag. `patients` and `providers` hold the domain record, each with a **nullable,
unique** `user_id`. A patient may exist with no account and be linked to one later without
touching clinical data.

### 3.2 Slots are pre-generated rows

The requirements say "provider slot reserved", "slot release workflows", and "waitlist
movement" without ever defining a slot.

**Decision.** A `provider_slots` table, one row per bookable interval, generated ahead from
provider availability. Booking is an atomic conditional update:

```sql
UPDATE provider_slots SET status = 'held'
 WHERE id = :slot_id AND status = 'free'
```

Zero rows affected *is* the conflict signal. Double-booking is prevented by the database, not
by application logic — which is what makes the Week 2 Temporal activity honest under
concurrency. Slot release becomes a status transition rather than a computation.

The alternative (deriving free slots from availability rules at query time) makes conflict
prevention a range-overlap problem and leaves "release the slot" with no row to release.

### 3.3 A visit is a separate entity from an appointment

The requirements treat "Appointment Scheduling Workflow" and "Visit & Service Workflow" as
distinct sections, and the analytics list wants *Completed Visits* and *Average Wait Time*
alongside *Appointments Booked*.

**Decision.** `appointments` is the booking — who, when, with whom. `visits` is what actually
happened — `checked_in_at`, `seen_at`, `completed_at` — created on check-in, 1:1 with an
appointment.

Consequences that justify the split: *Average Wait Time* is `avg(seen_at - checked_in_at)`
directly; a no-show is an appointment past its slot with no visit row, needing no extra
state; and every future appointment avoids carrying three null timestamp columns it will
never use. Two workflows also stop mutating one row.

### 3.4 MongoDB owns the audit trail

The mandated stack includes a NoSQL database; the requirements never say what for. Left
undecided it becomes a decorative stack item, which the evaluation criteria explicitly
penalise ("correct use of defined tech stack").

**Decision.** Mongo owns the audit trail — an explicit requirement ("audit trail for profile
and operational changes") and a genuine document-store fit: append-only, high volume, and a
before/after payload whose shape differs per entity and per action. Relational alternatives
are a JSON column (making Mongo pointless) or a table per entity (unmaintainable). The access
pattern — `(entity_type, entity_id, timestamp)` — is a document-store sweet spot.

Postgres remains the system of record for every piece of transactional state. Mongo later
also carries notification delivery records.

### 3.5 Departments belong to a clinic; providers span both

"Department and clinic management" and "multi-clinic operations" leave the hierarchy open.

**Decision.** `departments.clinic_id` — "Cardiology at Riverside" is a different operational
unit from "Cardiology at Northgate" in staffing and hours. Providers relate to departments
many-to-many via `provider_departments`.

This is the one place the design goes beyond the letter of the requirements. The justification
is a first-class query: "show available appointments this week" needs *which providers can be
booked at clinic X*, and a single FK breaks that the moment a provider covers two sites.

## 4. Structure

```
app/
  core/
    settings.py     extends the existing Settings with DSNs and JWT config
    clock.py        injectable now() - harness requirement 6
    logging.py      structured JSON logging
    registry.py     generic Registry - harness requirement 3
  db/
    base.py         DeclarativeBase, UUID + timestamp mixins
    engine.py       async engine factory (lazy; does not connect at import)
    session.py      async_sessionmaker and the FastAPI dependency
  modules/
    identity/       models.py (users), security.py (hashing, JWT), deps.py (require_role)
    patients/       models.py
    providers/      models.py (clinics, departments, providers, provider_departments,
                    provider_slots)
    scheduling/     models.py (appointments, visits, waitlist_entries)
  api/
    health.py       /health and /ready
    router.py       assembly
  main.py           app factory + lifespan
migrations/         alembic env.py and versions/
alembic.ini
```

Modules carry `models.py` only in this slice. Grouping by domain rather than by technical
layer means a feature's parts change together and live together, and it maps onto the
registries the harness's meta-tests will enumerate in Week 3.

## 5. Schema

Ten tables. Conventions: UUID primary keys (stable across event payloads, no sequence
contention); `DateTime(timezone=True)` throughout with `created_at`/`updated_at` on every
table; status columns as `Enum(..., native_enum=False, create_constraint=True)`, i.e. `VARCHAR`
+ `CHECK`, because native Postgres enums are painful to alter in migrations.
`create_constraint=True` is not optional: SQLAlchemy 2.0 defaults it to `False`, which
emits a bare `VARCHAR` accepting any string — keeping the migration-friendliness while
silently discarding the validation.

| Table | Key columns | Notes |
|---|---|---|
| `users` | `email` unique, `password_hash`, `role`, `is_active` | roles: `patient`, `provider`, `front_desk`, `admin` |
| `patients` | `mrn` unique, name, `date_of_birth`, contact, `user_id` nullable unique | may exist without an account (§3.1) |
| `providers` | name, `specialty`, `license_number` unique, `user_id` nullable unique, `is_active` | |
| `clinics` | `name`, `timezone`, address, `is_active` | `timezone` matters for slot generation |
| `departments` | `clinic_id`, `name`, unique `(clinic_id, name)` | scoped to a clinic (§3.5) |
| `provider_departments` | PK `(provider_id, department_id)` | many-to-many |
| `provider_slots` | `provider_id`, `clinic_id`, `department_id?`, `starts_at`, `ends_at`, `status`, `version` | see below |
| `appointments` | `patient_id`, `provider_id`, `slot_id`, `clinic_id`, `status`, `rescheduled_from_id?` | see below |
| `visits` | `appointment_id` unique, `checked_in_at`, `seen_at?`, `completed_at?`, `status` | 1:1 with appointment |
| `waitlist_entries` | `patient_id`, `provider_id?`, `department_id?`, window, `status` | Week 2 promotes from here |

### 5.1 `provider_slots`

`status` ∈ `free | held | booked | blocked`. Two database-level guarantees:

- `CHECK (ends_at > starts_at)`.
- An exclusion constraint preventing overlapping slots for one provider:
  `EXCLUDE USING gist (provider_id WITH =, tstzrange(starts_at, ends_at) WITH &&)`, which
  requires the `btree_gist` extension (created in the baseline migration). This makes a
  buggy slot generator fail loudly at write time instead of silently producing
  double-bookable inventory.

`version` is an optimistic-concurrency counter for the Week 2 workflow.

**`updated_at` needs a database trigger, not the ORM default.** Verified against the
live database: `onupdate=func.now()` fires on an ORM flush but **not** on a raw
`text("UPDATE ...")`. Since §3.2's atomic slot claim is exactly such a raw conditional
update, the baseline migration installs a `BEFORE UPDATE` trigger on every table
carrying `updated_at`. A trigger also covers hand-written statements and data
migrations, which a coding convention cannot.

**Refinement from the approved sketch:** slots do **not** carry `appointment_id`. Having both
`slots.appointment_id` and `appointments.slot_id` is a circular reference with two sources of
truth that can disagree. The link lives on `appointments.slot_id`, guarded by a partial unique
index so at most one *live* appointment can hold a slot:

```sql
CREATE UNIQUE INDEX ux_appointments_live_slot ON appointments (slot_id)
 WHERE status IN ('pending', 'confirmed');
```

The atomic claim in §3.2 is unaffected — it transitions `status`, and the index independently
guarantees the invariant even if application logic is wrong.

### 5.2 `appointments`

`status` ∈ `pending | confirmed | cancelled | rescheduled | completed | no_show | failed`.

`pending` is the initial state; **`confirmed` is reachable only after the whole Temporal
workflow succeeds**, and `failed` records a workflow that failed and was compensated. This is
the assignment's headline invariant — "appointment marked confirmed only after successful
processing" — expressed in the schema rather than left to a service method.

Rescheduling inserts a **new** row with `rescheduled_from_id` pointing at the old one, which
moves to `rescheduled`. History is preserved; the audit trail and analytics both need it.

## 5.3 What the database cannot enforce — Week 2's contract

Composite foreign keys make a slot claim *coherent* (the slot's provider and clinic must
match the appointment's). Three further invariants have no schema expression and must be
upheld by the booking, cancellation, and visit workflows. They are recorded here because
an invariant nobody wrote down is an invariant nobody maintains.

**1. Releasing a slot is a paired write.** `provider_slots.status` and appointment
liveness are two independently-writable facts. Verified: moving an appointment to
`rescheduled` frees it from the partial unique index — a second appointment may then
claim that `slot_id` — but the slot's own `status` stays `booked`, so it never reappears
in the `status = 'free'` query that booking actually uses. Every cancel, reschedule,
no-show and completion path must flip the appointment status **and** release the slot in
one transaction. This deserves an explicit integration test, not a convention.

**2. A slot in the past is still bookable.** Nothing stops an appointment claiming a slot
whose `starts_at` has already passed. A `CHECK (starts_at > now())` is not available —
Postgres requires check constraints to be immutable, and `now()` is not. The booking
activity must reject past slots itself.

**3. The optimistic-concurrency counter is not self-maintaining.** `provider_slots.version`
exists but nothing increments it. The atomic claim must be
`SET status = 'held', version = version + 1 WHERE id = :id AND status = 'free'`, not a
status flip alone, or the column is decoration.

## 6. Tooling

| Concern | Choice | Why |
|---|---|---|
| ORM | SQLAlchemy 2.0 async + `asyncpg` | consistent with FastAPI, Temporal, and Kafka clients all being async |
| Migrations | Alembic, async `env.py`, one baseline revision | the harness's per-run database needs a repeatable schema |
| Mongo | `pymongo` async driver | Motor reached its deprecation date in May 2026 |
| Redis | `redis.asyncio` | async |
| Password hashing | `pwdlib[argon2]` | argon2 by default; the maintained successor to passlib |
| JWT | `pyjwt` | minimal and sufficient |
| Lifespan in tests | `asgi-lifespan` (dev) | see §7.1 |

Settings gain Postgres/Mongo/Redis DSN components and JWT configuration, all under the
existing `SMARTHEALTH_` prefix, and `.env.example` is updated to match.

## 7. Harness constraints this slice makes real

`tests/README.md` documents three traps as inert-until-triggered. This slice triggers all
three, so each is handled deliberately rather than discovered.

### 7.1 `ASGITransport` does not run lifespan

Once the engine is created in a lifespan handler, T1 tests would exercise an app whose startup
never ran. Fix: add `asgi-lifespan` and wrap the `api_client` fixture in `LifespanManager`, so
T1 runs the real startup path.

This still needs **no containers**, because `create_async_engine` is lazy — it constructs a
pool without connecting. Startup succeeds offline; only a query would fail, and no T1 test
issues one.

### 7.2 `/health`'s `dict[str, str]` is an enforced response model

FastAPI validates the return annotation, so a nested or boolean field raises
`ResponseValidationError`. `/health` therefore stays exactly as it is — a flat liveness probe.
Readiness gets a **new** `/ready` endpoint with its own Pydantic response model, reporting
per-dependency status and returning 503 when a dependency is down.

### 7.3 `get_settings()` is an `lru_cache` singleton

The T3 session fixture builds `Settings(...)` directly from `isolation.as_env()` rather than
calling `get_settings()`, per the constraint already recorded in `tests/README.md`. Calling
the cached accessor from a session-scoped fixture would freeze a pre-override snapshot and
silently defeat isolation exactly where T3 depends on it.

## 8. Testing

| Tier | Coverage |
|---|---|
| T0 unit | DSN construction from settings; clock; registry; password hash/verify round-trip; JWT encode/decode including expiry and tamper rejection |
| T1 contract | `/health` unchanged; `/ready` returns 503 with a per-dependency breakdown when dependencies are unreachable (pointed at a dead port — no containers) |
| T3 integration `@docker` | `alembic upgrade head` against real Postgres; per-run database creation and teardown; every table present; the slot exclusion constraint actually rejects an overlap; the partial unique index actually rejects a second live appointment on one slot; Mongo audit round-trip; Redis ping |

The two constraint tests matter most: they assert that the *database* enforces the invariant,
which is the entire reason for choosing pre-generated slots.

A new case file `sys-002-ready-endpoint-reports-dependencies.yaml` adds the catalog's second
real entry.

`tests/core-paths.txt` gains `app/**` at the end of this slice, arming the `Stop` hook now
that there is feature code worth gating.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Async SQLAlchemy is fiddly (greenlet errors, lazy-load traps) | Session-per-request dependency; no lazy relationship loading — explicit `selectinload` where needed |
| Alembic autogenerate misses constraints | The baseline migration is hand-checked; the exclusion constraint and partial index are written explicitly, and T3 asserts both actually reject violations |
| `btree_gist` unavailable | Created in the baseline migration; the standard `postgres:16-alpine` image ships it |
| Scope creep into endpoints | Non-goals in §2 are explicit; the module tree deliberately has no router files |

## 10. Open questions

None blocking. Two to revisit with Week 2:

1. Slot generation cadence — a Celery beat job versus a Temporal cron workflow. Deferred
   until there is a scheduler; the table shape is unaffected either way.
2. Whether `waitlist_entries` needs a priority column. Deferred until the promotion policy
   exists; adding a column is cheaper than guessing a policy.
