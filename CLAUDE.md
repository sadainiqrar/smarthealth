# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**Week 1 complete.** On top of the foundation (settings, core utilities, async SQLAlchemy
with Alembic migrations, the ten-table domain schema, Mongo and Redis clients,
liveness/readiness) the business surface now exists: `POST /auth/login`; patient and
provider management with role-gated writes, paginated and filtered reads, and every
mutation audited to Mongo; a `create-user` CLI so a running system is demonstrable; and a
Dockerfile plus an `app` service behind a compose profile. Week 2 adds scheduling.

`README.md` carries the endpoint/role table. The design and the list of what was
deliberately deferred are in
`docs/superpowers/specs/2026-09-03-week1-management-apis-design.md`.

Commands that work today:

| Command | Purpose |
| --- | --- |
| `python -m pytest -m "not docker"` | fast tests — T0 unit, T1 contract, meta |
| `python -m pytest -m docker` | integration tests against the compose test stack |
| `python -m tests.runner.route_check` | validate and route the case catalog |
| `python -m ruff check app tests` | lint |
| `python -m app.cli create-user --email … --password … --role …` | create a login |
| `docker compose --profile app -f docker-compose.infra.yml up -d --wait` | the whole system in containers |

**Anything reaching Postgres from the host needs connection settings.** The *test* stack
(`--env-file .env.test`) publishes Postgres on an offset port (**15432**) so a dev and a
test stack can coexist, while `Settings` defaults to 5432. Run alembic and the CLI as:

```bash
SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth python -m alembic upgrade head

SMARTHEALTH_POSTGRES_PORT=15432 SMARTHEALTH_POSTGRES_DB=smarthealth python -m alembic check
```

Or copy `.env.example` to `.env` and set the offset ports there — `Settings` reads
`.env`. Without either, alembic fails with `ConnectionRefusedError` against 5432.

Note that `docker compose --profile app` starts the *dev* project, a different database
from the test stack — a user created against 15432 does not exist in the containerised
one. `README.md` spells this out.

## What this project is

SmartHealth is a training assignment: a backend for a fictional healthcare operations platform
(MediNova). It is delivered in two parts over five weeks, and is evaluated on architecture
clarity, correct use of the mandated tech stack, reliability/failure handling, and observability
maturity — not on feature count.

The full requirements live in `docs/requirements/`:

| File | Contents |
| --- | --- |
| `part-a-core-platform.md` | Part A — core platform: patients, providers, scheduling, visits, events, analytics |
| `part-b-genai-layer.md` | Part B — GenAI layer: ingestion/embeddings, retrieval, AI assistant, streaming |
| `execution-guidelines.md` | Week-by-week plan, submission rules, evaluation criteria |

The `.docx` originals sit alongside the extracts and are authoritative if the two ever disagree.

## Mandated tech stack

The stack is prescribed by the assignment, not chosen. Do not substitute alternatives (e.g. no
RQ instead of Celery, no Airflow instead of Temporal) without the user explicitly deciding to.

**Part A** — Python, FastAPI, PostgreSQL, a NoSQL DB, Redis, Celery workers with RabbitMQ,
Kafka + Schema Registry, Temporal for workflows.
**Part A observability** — Prometheus + Grafana, Jaeger, OpenTelemetry.
**Part B** — LangGraph / LangChain, an LLM provider (OpenAI / Groq / Anthropic), a vector DB.
**DevOps** — Docker, Docker Compose.

Note the deliberate split of responsibilities the stack implies, and keep it: Temporal owns
durable multi-step business workflows (booking, visit lifecycle), Kafka carries domain events
between modules, and Celery/RabbitMQ handles fire-and-forget background jobs (notifications,
analytics rollups). Reaching for the wrong one of these three is the most likely architectural
mistake in this project.

## Architectural constraints that drive the design

These are the requirements most likely to be violated by an obvious implementation:

- **Appointments confirm only after the whole workflow succeeds.** Slot reservation, conflict
  checks, notification scheduling, and billing pre-check all happen before an appointment is
  marked confirmed. Partial failures must leave scheduling state uncorrupted — this is why
  Temporal is in the stack.
- **Everything asynchronous must be idempotent.** Retries, duplicate prevention, and recovery
  from mid-flight failure are explicit requirements for the visit/service workflow and all
  event consumers. Assume at-least-once delivery everywhere.
- **Background work must not block user-facing flows.** This applies to Part A's notification
  and analytics paths and, in Part B, to LLM calls specifically — long AI responses stream or
  go async rather than holding a request.
- **Cancellation and reschedule are first-class flows**, not the inverse of booking: they
  trigger slot release, waitlist movement, refund/billing updates, and notifications.
- **Every critical flow must be diagnosable.** Booking, provider availability sync, billing,
  reminders, background workers, and (Part B) the retrieval pipeline, streaming responses, and
  external LLM calls each need to be traceable when they fail.
- **Roles are fixed:** `patient`, `provider`, `front desk staff`, `admin`. Profile and
  operational changes need an audit trail.

## Working conventions

- Work is sequenced by week (see `execution-guidelines.md`). Week N's foundations are expected
  to be reviewed before Week N+1 starts, so prefer completing and solidifying the current
  week's scope over starting ahead.
- The assignment requires meaningful commits and logical module separation — the git history is
  part of what gets evaluated.
- A PRD is a required deliverable for both parts, with traceability from features to
  deliverables. Design decisions, assumptions, and tradeoffs belong in `docs/`, not only in
  code comments.

## Testing

Design: `docs/superpowers/specs/2026-09-01-testing-harness-design.md`. Practice:
`tests/README.md`.

Five tiers — `unit`, `contract`, `workflow`, `integration`, `journey` — selected by pytest
marker. **Choose the cheapest tier that can prove the property.** Most reliability
invariants (appointment confirms only after the workflow succeeds; partial failure leaves
no orphaned slot) belong in `workflow`, which runs against the Temporal SDK's time-skipping
environment in about a second — not in `journey`.

Cases are YAML under `tests/cases/`, authored with the **smarthealth-testcase** skill.
`tests/runner/schema.py` is the single validation authority. A case that asserts nothing is
a hard error, never a silent skip — and declaring `steps` is not sufficient: a case must
carry a case-level `expect`, a per-step `expect`, or an `await` step.

A `Stop` hook blocks the session when a path in `tests/core-paths.txt` changes without a
validated case. Waive with `E2E_WAIVE="<reason>"` — logged to `tests/waivers.log`, not
silent.

The hook is **armed**: `app/api/**`, `app/core/**`, `app/db/**`, `app/modules/**`, and
`migrations/**` are core paths. Changing any of them without adding a validated case
blocks the session. `app/settings.py` and `app/main.py` are deliberately excluded — they
are wiring that changes whenever a module is added, so gating them would fire constantly
without adding signal.

**Harness requirements on application code** — honour these as modules land:

1. Topic, queue, and task-queue names come from `Settings.topic()/queue()/task_queue()`,
   never string literals — the isolation layer namespaces a shared stack through them.
2. LLM and embedding clients come from a provider factory keyed on `Settings.llm_mode`.
3. Kafka consumers, Temporal workflows, and Celery tasks register in enumerable registries
   so meta-tests can discover them.
4. Consumers take an explicit idempotency key.
5. The OpenTelemetry tracer provider stays swappable.
6. Time comes from an injectable `now()` provider, never `datetime.utcnow()` inline.
7. Every service exposes a readiness endpoint.

## Domain model

Design and rationale: `docs/superpowers/specs/2026-09-02-week1-foundation-design.md`.

Four decisions the requirements left open, each easy to get wrong:

1. **A patient is not a user.** `users` is auth identity; `patients`/`providers` are
   domain records with a nullable, unique `user_id`. Front-desk staff register walk-ins
   who have no credentials.
2. **Slots are pre-generated rows.** Booking is
   `UPDATE provider_slots SET status='held', version=version+1 WHERE id=:id AND
   status='free'` — zero rows affected *is* the conflict.
3. **A visit is separate from an appointment.** Appointment = the booking; visit = what
   happened. Average Wait Time is `seen_at - checked_in_at`; a no-show is an appointment
   with no visit.
4. **Mongo owns the audit trail.** Postgres is the system of record for all
   transactional state.

`appointments.status` starts at `pending`. **`confirmed` is reachable only after the
whole booking workflow succeeds** — the assignment's headline invariant, enforced by the
schema default and by a CHECK that a confirmed appointment must hold a slot.

### What the database guarantees

Proven by integration tests, not by convention:

- A provider cannot have two overlapping slots (GiST exclusion constraint).
- At most one *live* appointment may hold a slot (partial unique index) — partial, so
  cancelling frees it.
- An appointment's slot must belong to the provider and clinic it names, and a
  department must belong to its clinic (composite foreign keys). Every single-column FK
  can be satisfied while the booking is still nonsense; these catch that.
- `updated_at` moves on raw SQL updates (`BEFORE UPDATE` trigger using
  `clock_timestamp()`). SQLAlchemy's `onupdate` does not fire for raw statements, and
  `now()` would record when the transaction began rather than when the row changed.

### What it cannot guarantee — Week 2's contract

- **Releasing a slot is a paired write.** Moving an appointment out of `pending`/
  `confirmed` frees the partial index, but `provider_slots.status` stays as it was, so
  the slot never reappears in a `status = 'free'` query. Cancel, reschedule, no-show and
  completion must flip both in one transaction.
- **A past slot is still bookable.** `CHECK (starts_at > now())` is impossible — Postgres
  requires check constraints to be immutable. The booking activity must reject it.
- **`version` is not self-incrementing.** The claim statement must say
  `version = version + 1`, or the column is decoration.
