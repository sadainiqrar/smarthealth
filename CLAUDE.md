# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**Week 1 complete.** On top of the foundation (settings, core utilities, async SQLAlchemy
with Alembic migrations, the ten-table domain schema, Mongo and Redis clients,
liveness/readiness) the business surface now exists: `POST /auth/login`; patient and
provider management with role-gated writes, paginated and filtered reads, and every
mutation audited to Mongo; a `create-user` CLI so a running system is demonstrable; and a
Dockerfile plus an `app` service behind a compose profile.

**Week 2 complete — all five slices have landed.** Slice 1: a Temporal worker
process (`app/worker.py`, the `worker` compose service) with workflow/activity registries,
a canary workflow as its readiness probe, and Temporal in `/ready`. Slice 2, the booking
workflow: `POST /appointments` (202) starts `BookAppointmentWorkflow`, which creates the
`pending` row, validates, claims the slot, runs the billing pre-check and reminder
scheduling (both **stand-ins** until Week 3) and confirms last, compensating on any failure;
`GET /appointments/{id}` reads the result. Slice 3, the visit lifecycle:
`POST /appointments/{id}/check-in`, `/seen` and `/complete` write the visit
**synchronously**, in the request's transaction under the appointment row lock, and answer
200 (409 on an illegal step); `GET /appointments/{id}/visit` reads it. A `VisitWorkflow`,
started when a booking confirms, is only the durable no-show timer — the database records
human actions, the workflow owns time (PRD D-12; an earlier signal-driven design was
replaced after review because it lost check-ins). Billing and follow-up on completion are
Week 3. Slice 4, cancel, reschedule and the waitlist: `POST /appointments/{id}/cancel`
writes the appointment and its slot **together** and answers 200, then best-effort starts
`CancelFollowUpWorkflow` (stop the visit timer, refund, offer the slot to the waitlist) — if
that start fails the follow-up is lost (PRD R-17, closed by the Week 3 outbox);
`POST /appointments/{id}/reschedule` answers 202 and `RescheduleWorkflow` **books the new
slot before it releases the old one**; `POST /waitlist` and `GET /waitlist` manage entries,
and a release promotes up to three matching entries as child bookings
(`booking-wl-<entry>`). The refund (`cancel.refund`) is a stand-in, as are the billing
pre-check and reminder scheduling, and no notification or domain event exists for cancel or
reschedule yet (Week 3). Code in `app/modules/scheduling/`. The schema now has four
migrations, `0001_baseline` to `0004_live_reschedule_unique`; slice 4 added the last.
Slice 5, patient
self-service (PRD R-5, closed): `GET /appointments` lists a patient's own appointments —
forced server-side to their own record, 403 for another `patient_id`, 404 for a login with
no linked record — or, for provider, front desk and admin, filters by `patient_id`,
`provider_id`, `status` and a slot window; newest slot first, slot-less rows last, and each
item carries the slot's `starts_at`. `python -m app.cli link-patient --email … --mrn …`
links a patient login to its record (audited), which `GET /patients/me` needs. The demo
client gained an Appointments page with cancel. All five slices are designed in
`docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md`.

**Week 3 is in progress**, in seven slices designed with Weeks 4–5 in
`docs/superpowers/specs/2026-11-10-weeks3-5-architecture-design.md` (three services: Part
A stays one monolith; Part B is `knowledge`, the RAG service, and `assistant`; they share
Kafka, Envoy and the observability stack and never a store — decisions D-17 … D-31).
**Slice 1, the observability base, is built:** `app/core/telemetry.py` installs one tracer
and meter provider per process (first install wins — the suite installs in-memory ones
first, `tests/harness/telemetry.py`); FastAPI and SQLAlchemy are instrumented; every
Temporal client carries `TracingInterceptor`, so a workflow's run and activities join the
trace that started it; the worker exports Temporal SDK metrics over OTLP/gRPC; JSON log
lines carry `trace_id`/`span_id`; compose runs `otel-collector` → `jaeger` / `prometheus`,
and `grafana` with provisioned datasources and a *Service overview* dashboard (`ops/`).
Export is off unless `SMARTHEALTH_OTEL_ENDPOINT_HTTP` (and `_GRPC`) are set.
**Slice 2, sessions, is built:** access tokens are ten-minute **Ed25519** JWTs naming a
server-side session in Redis (`app/modules/identity/sessions.py`); `require_role` refuses a
token whose session is gone; `POST /auth/refresh` rotates a single-use refresh token (a
spent one presented again ends the session), `/auth/logout` and `/auth/logout-all` end
sessions, `GET /.well-known/jwks.json` publishes the public key for the Part B services;
login throttling (`app/core/ratelimit.py`) locks an address after five failures while
answering exactly like a wrong password; booking and reschedule share a per-user limit
(429). **Every test token needs a session**: mint with `tests/harness/auth.py:issue_token`
(contract tier, in-memory store) or the T3 `token_for` fixture (real Redis) — never bare
`create_access_token`. **Slice 3, the event backbone, is built:** every domain change writes its event through
`app/events/outbox.py:emit` **in its own transaction** (the outbox, D-21); `python -m
app.relay` (compose `relay`) publishes as Avro under Schema Registry, per-key in order, at
least once; `app/events/consumer.py` is the consumer runtime (dedupe in `processed_events`,
retry, `<topic>.dlt`); topics in `app/events/catalog.py`, schemas in `contracts/events/`
(a meta-test keeps them in step; no clinical field names). Broker topic auto-creation is
**off** — `python -m app.relay --setup` creates topics and registers schemas. **Slice 4, Kafka → Temporal and billing, is built:** `python -m app.consumers` (compose
`consumers`) runs `app/modules/scheduling/consumers.py` — `appointments.cancelled` starts the
cancel follow-up (**PRD R-17 closed**: the cancel request no longer calls Temporal),
`providers.state` with `is_active=false` starts `ProviderDeactivationWorkflow` (**R-25
closed**), `visits.completed` starts `PostVisitWorkflow` (the charge). `app/modules/billing/`
is one state-machine row per appointment over `services/payments_mock` (compose
`payments-mock`, test port 18090; `POST /_chaos` makes it misbehave) behind a circuit
breaker; the pre-check authorises (a decline compensates), compensation/cancel/reschedule
release, post-visit captures. T3 round trips run the relay and consumers in process
(`scheduling_helpers.event_pipeline`), so they wait for workflows that Kafka starts. **Slice 5, Celery notifications, is built:** `app/celery_app.py` (RabbitMQ vhost `part-a`,
queues `notifications` with a dead-letter queue and `maintenance`, `acks_late`, every task in
`TASKS`); `app/modules/notifications/` — the `notifications.patient-messages` consumer queues one
row per event (unique key) and enqueues the send **after commit**; the send task claims the row
first, throttles per patient, retries with backoff, dead-letters; email via SMTP to **Mailpit**
(compose `mailpit`, inbox :8025, test 18025/11025), SMS a logging fake. The reminder is timed by
`VisitWorkflow` (behind `workflow.patched("visit-reminder")`, D-26). Beat: requeue stuck
notifications, the stale-pending reconciler (**PRD R-9 closed**), outbox purge. Compose
`celery-worker`, `celery-beat` (profile app). **Slice 6, analytics and the dashboards, is built:** `app/modules/analytics/` (the
`analytics.daily-rollup` consumer into `analytics_daily`, `GET /analytics/summary` and
`/timeseries`, a nightly reconcile against `scheduling/public.py:daily_counts`); Grafana has
*Service overview*, *Async backbone* and *Platform analytics* (Postgres datasource as the
read-only `analytics_reader`, migration 0008) and six provisioned alert rules; Prometheus also
scrapes RabbitMQ and `kafka-exporter`. Schema evolution happened once: cancelled and no-show
are v2 (optional `clinic_id`). **Week 4 slice 1, knowledge retrieval, is built:** `services/knowledge/corpus/` (fictional
MediNova documents, Markdown + one PDF, each with an `audience`) is ingested by
`knowledge_service.ingest` (heading-then-size chunks, fastembed BGE-small + BM25, Qdrant;
idempotent and versioned) and searched hybrid with the caller's audience and a relevance floor.
**Embedding modes:** tests replay `services/knowledge/eval/fixtures/embeddings.jsonl` (fixture);
after changing the corpus, golden set or model run
`cd services/knowledge && KNOWLEDGE_EMBEDDING_MODE=record python -m knowledge_service.eval`
(it prints recall@5 and MRR) and commit the fixture file. Compose runs `live`.
**Week 4 slice 2, the clinical record, is built — Week 4 is complete:** `app/modules/clinical/`
(versioned notes, prescriptions, test orders against `catalogue.py`, uploaded reports in a
volume behind `DocumentStore` with text extracted by the Celery task `clinical.extract_document`
on the `documents` queue). **A visit completes only with a signed note**, and seen/complete are
the appointment's own provider's (**PRD R-14 closed**); records are read by role and care
relationship (`scheduling/public.py`). Tests that complete a visit through the API need a
provider login linked to the record and a signed note (see `test_visit_round_trip.py`).

**Week 5, the assistant, is built — all five weeks are complete.** `services/assistant/`
(`assistant_service`): a LangGraph graph per conversation (`agent.py`: begin → chat | flow ⇄
ask). **Guided flows** (`flows/`, fifteen use cases by role plus two drafting flows) are
deterministic state machines over Part A's REST API **with the user's own token**; each
question is a LangGraph `interrupt`, checkpointed in `assistant-db` (Postgres). **The model
never writes** (PRD D-32): it may only `search_knowledge` and `start_flow`; every write is a
flow's confirmation (`Confirm` RPC). Red flags (`guard.py`) are answered deterministically.
The model comes from `llm.py` (`ASSISTANT_LLM_MODE` auto|off|fixture|record|live; provider,
model and key from `ASSISTANT_LLM_PROVIDER` / `_MODEL` / `_API_KEY` — Groq
`openai/gpt-oss-120b` in use); without one, `offline.py` routes to a flow or quotes the
documents. Also: threads (`conversations.py`), per-turn usage and drafts (`usage.py`), reports
written in the background and streamed (`reports.py`), metrics (`metrics.py`) for the Grafana
*Assistant* dashboard. Part A gained `GET /users` (admin directory) and `POST /notifications`
(send an approved message). **Tests never call a live model**: they script one
(`Scripted` in `tests/tiers/t1_contract/test_assistant_agent.py`) against a fake Part A
(`tests/harness/fake_part_a.py`). **The real key lives in `.env` / `.env.test`; `.env.test` is
tracked — never stage it while it holds a key.** The demo client has Assistant, Reports and AI
usage pages built from shadcn AI Elements (Tailwind v4, React 19, a Connect gRPC-Web client
generated by `npm run gen:proto`).

**Slice 7, the Part B skeletons, is built — Week 3 is complete.** `services/knowledge`
(`knowledge_service`: gRPC `Search` on :50052, its own Postgres `knowledge-db` and `qdrant`, a
provider directory built from `providers.state`) and `services/assistant` (`assistant_service`:
server-streaming `Chat` on :50051 echoing with citations from knowledge, resumable turns and a
revoked-session deny-list in its own `assistant-redis`). Contracts in `contracts/proto/`;
`python scripts/gen_proto.py` generates each service's own bindings into `services/<name>/gen`
(namespace packages, no `__init__.py`; lint excludes them). Both verify Part A's tokens against
`/.well-known/jwks.json`. `envoy` (:8088) is the front door: gRPC-Web to Part B, `/api/*` to Part
A; `scripts/grpcweb_smoke.py` speaks gRPC-Web through it. Compose profile `partb`. Part B tests
import `tests.harness.partb` for the services' paths; `tests/meta/test_service_independence.py`
fails on any import, setting or compose variable that crosses services. Week 3's Part A scope is complete: every Week 2 stand-in
(billing pre-check, reminder scheduling, refund) is now real, and cancel/reschedule have events.
Not built in Week 2, by decision: a provider availability generator (slots are seeded, PRD
`PART-A-FR-2.1`), clinic/department endpoints (PRD `PART-A-FR-1.5`), and handling a
deactivated provider's existing appointments (PRD R-25, Week 3 — new bookings for one are
already refused, R-24). Closed after the Week 2 audit (2026-10-29): a patient can no
longer hold two overlapping appointments (R-23), and Swagger `/docs` has an Authorize button.

`README.md` carries the endpoint/role table and the architecture diagrams. The design and
the list of what was deliberately deferred are in
`docs/superpowers/specs/2026-09-03-week1-management-apis-design.md`.

Two documents carry the things code cannot state, and are the first stop for "why":

| Document | Use it for |
| --- | --- |
| `docs/PRD.md` | Numbered requirement ids (`PART-A-FR-1` …) that `tests/cases/*.yaml` reference, the design decisions with their rejected alternatives (§8, D-1 … D-16), and the known gaps with dispositions (§9, R-1 … R-22) |
| `docs/ARCHITECTURE.md` | Module-by-module breakdown with import rules, data flows for every built endpoint, the **built** booking workflow (§3.2), visit lifecycle (§2.9, §3.2a) and cancel/reschedule/waitlist flows (§3.3, which carries their data flows), and the **designed** event flows — the Temporal/Kafka/Celery split, the event catalogue |

**Treat every document here as a source, not as proof.** Docs drift from code, and a stale
claim is worse than a missing one because it gets trusted. Two real examples found on
2026-09-13: the testing-harness spec asserted an appointment status (`pending_failed`) that
does not exist in the enum, and the harness-requirements list below reads as present tense
when it is forward-looking. **Grep before asserting** that something exists.

Commands that work today:

| Command | Purpose |
| --- | --- |
| `python -m pytest -m "not docker"` | fast tests — T0 unit, T1 contract, T2 workflow (Temporal time-skipping server), meta |
| `python -m pytest -m docker` | integration tests against the compose test stack |
| `cd frontend && npm run dev` | the demo client's Vite dev server on :5173, proxying `/api` to the backend on :8000 |
| `cd frontend && npm test` | the demo client's Vitest suite |
| `python -m tests.runner.route_check` | validate and route the case catalog |
| `python -m ruff check app tests services` | lint (generated bindings excluded) |
| `python scripts/gen_proto.py` | regenerate each Part B service's gRPC bindings from `contracts/proto/` |
| `docker compose --profile app --profile partb -f docker-compose.infra.yml up -d --build --wait` | Part A and the Part B skeletons together |
| `python -m app.cli create-user --email … --password … --role …` | create a login |
| `python -m app.cli link-patient --email … --mrn …` | link a patient-role login to the patient record with that MRN (audited as `system:cli`; refuses a non-patient login, an unknown email or MRN, or a record or login already linked elsewhere; repeating it is a no-op) |
| `celery -A app.celery_app worker -Q notifications,maintenance` / `celery -A app.celery_app beat` | the Celery worker and schedule |
| `python -m app.consumers` | run every registered Kafka consumer (the workflows that follow events) |
| `python -m app.relay [--setup]` | publish outbox rows to Kafka until stopped; `--setup` creates topics and registers schemas, then exits |
| `python -m app.cli republish` | queue a state event for every clinic, department and provider (backfill a downstream copy) |
| `python -m app.cli dlt-replay --topic … [--limit N]` | re-publish a topic's dead letters once their cause is fixed |
| `python -m app.cli deactivate-user --email …` | stop a login authenticating and end every live session it has |
| `python -m app.cli generate-signing-key` | print a fresh Ed25519 key for `SMARTHEALTH_JWT_PRIVATE_KEY` (the committed default is a development key, refused when `SMARTHEALTH_ENVIRONMENT=production`) |
| `python -m app.cli seed --password … [--patients N] [--clear]` | demo clinic network — clinics, departments, providers, patients, slots. Idempotent. Seeds **no** appointments/visits/waitlist: those are Week 2 workflow outputs |
| `python -m app.worker [--check]` | run the Temporal worker; `--check` executes the canary workflow and exits 0/1 (the compose healthcheck, connect and canary bounded to 8 s) |
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

`frontend/` is a demo client (React + TypeScript) for the HTTP API, not a graded deliverable
and not a requirement — see `docs/superpowers/specs/2026-10-28-frontend-demo-client-design.md`.
The backend does not depend on it.

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

- **This is a modular monolith, and both import rules are enforced by meta-tests**
  (`tests/meta/test_import_boundaries.py`), not by convention. *Vertical:* nothing outside
  the HTTP layer may reach FastAPI/Starlette, directly or transitively. *Horizontal:*
  `app/modules/<x>` may not import `app/modules/<y>` — `service.py`, `models.py` and
  `schemas.py` take no exception at all, and every other edge is an explicit pair in
  `CROSS_MODULE_ALLOWED`: nine router-layer auth edges onto `identity`, two onto
  `patients.public` and two onto `providers.public`. When a module needs another's data,
  the answer is a published interface or a domain event, never a direct import. Booking was
  the first real test, and it held: `scheduling.activities` (the patient check, and the
  inactive-provider check) and `scheduling.router` (patient ownership, and hiding an inactive
  provider's slots) reach `app/modules/patients/public.py` and
  `app/modules/providers/public.py` through declared pairs, while `scheduling/service.py`
  takes no cross-module import — the activity does those checks and hands the service ids
  only (the service supplies the slot's provider id, `slot_provider_id`). Decisions D-8 and D-15 in `docs/PRD.md`; rationale in
  `docs/ARCHITECTURE.md` §1.3.
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

## Three harnesses, three different things

"Harness" is used for three separate systems in this repository. They are at different
stages, and conflating them misleads — including in conversation with a reviewer.

| Harness | What it is | Status |
| --- | --- | --- |
| **Agent harness** | Context and tooling for the coding agent: this file, `.claude/skills/`, the `Stop` hook, `.claude/settings.json`. Spec §10, "Agent-facing layer" | **Built**, in daily use |
| **Test harness** | Five tiers, run isolation, the case catalog, the gate. Spec §§5–8, §10.2 | **Built** — 1220 fast tests (plus 1 skip) in ~78s (78.16s on a run of `python -m pytest -m "not docker" -q`) and 255 docker-lane tests (`-m docker`, ~7 min with the full dev stack also running), measured 2026-11-10; 80 cases; the journey tier has no cases yet |
| **AI harness** | LLM determinism: `SMARTHEALTH_LLM_MODE` ∈ `fixture\|record\|live`, and the offline judge. Spec §9 and §11 | **Built in part (Week 5).** The assistant's model factory (`services/assistant/assistant_service/llm.py`, `ASSISTANT_LLM_MODE`) has fixture, record and live modes, and the knowledge service replays recorded embeddings; tests script the model instead of recording it. No LLM fixture is recorded yet, and the offline judge is not built |

The AI harness is *specified in detail* and its expensive-to-retrofit seams already exist
(`Settings.llm_mode`; the `ai` step kind and `judge` block in `tests/runner/schema.py`, with
a cross-field rule binding each to the other). Week 5 built the part the assistant needs:
its own model factory (`services/assistant/assistant_service/llm.py`, with LangChain and
`langchain-groq` in that service's `requirements.txt`, not Part A's `pyproject.toml`). The
case runner's `ai` steps and the offline judge are still not implemented, and no LLM fixture
is recorded: do not describe those as working.

**Harness requirements on application code** — these are *forward-looking constraints*, to
honour as each module lands, not descriptions of code that exists. Each is tagged with the
harness it serves.

| # | Requirement | Serves |
| --- | --- | --- |
| 1 | Topic, queue, and task-queue names come from `Settings.topic()/queue()/task_queue()`, never string literals — the isolation layer namespaces a shared stack through them | Test |
| 2 | LLM and embedding clients come from a provider factory keyed on `Settings.llm_mode` | AI |
| 3 | Kafka consumers, Temporal workflows, and Celery tasks register in enumerable registries so meta-tests can discover them | Test |
| 4 | Consumers take an explicit idempotency key | Test |
| 5 | The OpenTelemetry tracer provider stays swappable | Test |
| 6 | Time comes from an injectable `now()` provider, never `datetime.utcnow()` inline | Test |
| 7 | Every service exposes a readiness endpoint | Test |

**5**, **6** and **7** are satisfied today, **1** for Temporal, Kafka and RabbitMQ, **3** for Temporal Kafka consumers
(`app/events/consumer.py:CONSUMERS`, enumerated by `tests/meta/test_consumers_idempotent.py`)
and Celery tasks (`app/celery_app.py:TASKS`, `tests/meta/test_celery_tasks.py`), and **4** in the
consumer runtime (`processed_events`, keyed on `event_id`); the rest await the code they
constrain.

- **1** — for Temporal only: the worker and the booking router take the queue from
  `Settings.task_queue()` (`app/worker.py`, `app/modules/scheduling/router.py`, with
  `app/core/temporal.py:TASK_QUEUE`); and Kafka: every topic and subject goes through
  `Settings.topic()` (`app/events/kafka.py:broker_topic`); and RabbitMQ: every queue through
  `Settings.queue()` (`app/celery_app.py:queues`).
- **3** — for Temporal only: `WORKFLOWS` / `ACTIVITIES` in `app/core/temporal.py`, populated
  solely by `app/components.py`, and `tests/meta/test_temporal_registry.py` discovers every
  `@workflow.defn` / `@activity.defn` in `app/` and fails if one is unregistered. Registered:
  the canary, `BookAppointmentWorkflow`, `VisitWorkflow`, `CancelFollowUpWorkflow`, `RescheduleWorkflow`, and the thirteen scheduling activities (seven `booking.*`, two `visit.*`, `cancel.refund`, `reschedule.complete`, `waitlist.candidates`, `waitlist.fulfil`). Kafka consumers
  and Celery tasks have no registries yet.
- **5** — `app/core/telemetry.py`: `build_telemetry` takes span processors and metric
  readers, and `install` lets the first install win, so the suite's in-memory providers
  (`tests/harness/telemetry.py`, imported by `tests/conftest.py` before the app) replace
  the real exporters everywhere.
- **6** — `app/core/clock.py`.
- **7** — `app/api/health.py` for the API; the worker has no HTTP endpoint, and its
  readiness probe is `python -m app.worker --check`, the `worker` service's healthcheck.

## Domain model

Design and rationale: `docs/superpowers/specs/2026-09-02-week1-foundation-design.md`.

Four decisions the requirements left open, each easy to get wrong:

1. **A patient is not a user.** `users` is auth identity; `patients`/`providers` are
   domain records with a nullable, unique `user_id`. Front-desk staff register walk-ins
   who have no credentials.
2. **Slots are pre-generated rows.** Booking is
   `UPDATE provider_slots SET status='held', version=version+1 WHERE id=:id AND
   status='free' AND starts_at > :now` — zero rows affected *is* the conflict.
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
- At most one *live* appointment may replace any given appointment
  (`ux_appointments_live_reschedule`, migration `0004`, a partial unique index on
  `rescheduled_from_id`). Two reschedules of one appointment, accepted together, otherwise
  both confirm and leave the patient two appointments for the one they moved, with every other
  constraint satisfied. Unlike `ux_appointments_live_slot` it is declared in the model, so
  `alembic check` tracks it.
- An appointment's slot must belong to the provider and clinic it names, and a
  department must belong to its clinic (composite foreign keys). Every single-column FK
  can be satisfied while the booking is still nonsense; these catch that.
- `updated_at` moves on raw SQL updates (`BEFORE UPDATE` trigger using
  `clock_timestamp()`). SQLAlchemy's `onupdate` does not fire for raw statements, and
  `now()` would record when the transaction began rather than when the row changed.

### What it cannot guarantee — and where the code does instead

- **Slot status must agree with appointment status, in one transaction** (PRD R-2,
  restated in Week 2). Leaving `pending`/`confirmed` frees the partial index but not
  `provider_slots.status`. **Closed in code for every path:** confirm → `booked`
  (`confirm_booking`); failed → `free` (`fail_booking`); cancelled → `free`
  (`cancel_appointment`) and rescheduled → `free` (`complete_reschedule`), both through
  `_release_slot`, with `version = version + 1`; completed and no-show write no slot at all,
  by design (their slot is in the past). A cancelled or rescheduled appointment keeps its
  `slot_id`, as a failed one does (PRD R-10). Slot status is written **only** in
  `app/modules/scheduling/service.py` — `tests/meta/test_slot_writes.py` fails if anything
  else in `app/` writes it, so any new release path goes there and must flip both halves in
  one transaction. Proof per path: PRD R-2.
- **A patient can hold two overlapping appointments** with different providers, as far as
  the database knows — every slot constraint is about one provider's time (PRD R-23). Closed
  in code: `claim_slot` takes a patient-keyed `pg_advisory_xact_lock` and refuses a slot
  overlapping another live appointment of the patient (`_ensure_patient_is_free`), the
  reschedule predecessor excepted.
- **A deactivated provider** is refused by `validate_booking` and hidden from `GET /slots`
  through `providers.public` (R-24); their *existing* appointments are untouched (R-25,
  Week 3).
- **A past slot is still bookable** at the database level — `CHECK (starts_at > now())` is
  impossible, Postgres requires check constraints to be immutable. Closed in code (R-3):
  `service.ensure_slot_bookable`, and a `starts_at > now` predicate in `claim_slot`'s UPDATE.
- **`version` is not self-incrementing.** Closed in code (R-4): every slot write in
  `scheduling/service.py` says `version = version + 1`. Any new slot write must too.
