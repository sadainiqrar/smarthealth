# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

**No application code exists yet.** This repository currently contains only the assignment
requirements. The build/test/run commands below do not exist until the corresponding scaffolding
is created — do not invent them, and update this file when they land.

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
