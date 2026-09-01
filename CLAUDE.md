# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**No feature code exists yet.** The repository holds the assignment requirements and the
testing harness spine (`tests/`, `docker-compose.infra.yml`, `.claude/`). Application
modules land week by week — do not invent build/run commands for services that do not
exist, and update this file when they do.

Commands that work today:

| Command | Purpose |
| --- | --- |
| `python -m pytest -m "not docker"` | fast tests — T0 unit, T1 contract |
| `python -m pytest -m docker` | tests needing the compose test stack |
| `python -m tests.runner.route_check` | validate and route the case catalog |

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
