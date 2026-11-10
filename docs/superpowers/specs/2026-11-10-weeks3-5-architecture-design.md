# Weeks 3–5 — architecture: events, observability, and the Part B services

Date: 2026-11-10. Status: **design, nothing in it is built.** Part A Week 2 is the
starting point (`CLAUDE.md`, PRD §10).

This document fixes the shape of everything after Week 2 *before* Week 3 code is written,
because several Week 3 choices — what an event carries, how a token is verified, how a
trace crosses a process — are decided by what Weeks 4–5 need. Each later slice gets its own
detailed design; this one settles the boundaries and the contracts between them.

New decisions are numbered **D-17 onward**, continuing PRD §8, and are copied into the PRD
when the slice that implements each one lands — not before, so the PRD keeps describing what
exists.

---

## 1. The system: three services

| Service | Weeks | What it is |
| --- | --- | --- |
| **Part A** (`app/`) | 1–3 | The core platform. A modular monolith with one database — unchanged by this design (D-8) |
| **`knowledge`** (`services/knowledge/`) | 4 | Turns documents and provider data into searchable knowledge; answers `Search` |
| **`assistant`** (`services/assistant/`) | 5 | Conversations, tools, drafts, reports; streams answers to the browser |

**D-17 — Part B is two services, split by data ownership and workload.** `knowledge` is
batch, CPU-heavy work (PDF parsing, embedding) that spikes when a topic is replayed;
`assistant` is interactive, I/O-bound work holding many long streams. In one process a
re-index would slow every live chat. They store nothing in common (vectors and documents vs
conversations and drafts). *Rejected:* one Part B service (the coupling above); more than
two — report and draft generation need the assistant's tools, LLM client and conversation
context, so splitting them out buys chatty synchronous calls and no ownership. They run as a
second **process** of the assistant (`assistant-worker`), not a second service.

**D-18 — Part A stays one service.** Restates D-8 for the multi-service era: Part A's
reliability guarantees are database constraints (the GiST exclusion, the partial unique
indexes, the composite foreign keys between slots, appointments, providers and clinics), and
a constraint cannot cross a service boundary.

### 1.1 Shared channels, owned stores

**D-19 — Services share the channels they talk and are watched through, never a store
that holds state.**

| Shared infrastructure | Owned by exactly one service |
| --- | --- |
| Kafka + Schema Registry (the event bus) | Postgres database |
| Envoy (the edge proxy) | Redis instance(s) |
| OTel Collector, Jaeger, Prometheus, Grafana | Qdrant (knowledge only), MongoDB (Part A only) |
| | RabbitMQ **vhost and user** — neither service can see the other's queues |
| | Temporal namespace / task queues (Part A only; Part B uses no Temporal) |

| | Part A | knowledge | assistant |
| --- | --- | --- | --- |
| Postgres | `postgres` (exists) | `knowledge-db` | `assistant-db` |
| Redis | `redis` (state) + `redis-cache` | — | `assistant-redis` |
| Other stores | MongoDB (audit); MinIO bucket `part-a-documents` (uploaded reports, §9.6) | Qdrant | — |
| RabbitMQ vhost | `/part-a` | `/knowledge` | `/assistant` |
| Celery workers | notifications, beat | ingestion | reports, drafts |

Temporal's own persistence uses the `postgres` container today; that is Temporal's
infrastructure, reached by no Part B service, and stays.

**No lock, transaction or table crosses a service.** Part A's row locks, advisory locks and
paired writes stay inside its database. Between services there are three paths only:

1. **Events (Kafka)** — eventually consistent, pulled by each consumer at its own pace.
2. **Synchronous API calls** — REST into Part A, gRPC between Part B services; always to the
   owning service's API, never its tables.
3. **Sagas** if a flow ever spans services — never a distributed transaction. None is
   needed by any use case below: the one cross-service write (booking through the
   assistant) is a single Part A API call that Part A's own workflow makes reliable.

### 1.2 Repository layout and how the boundaries are enforced

```
app/                      Part A (unchanged location)
services/knowledge/       Python package `knowledge`, its own pyproject, Dockerfile, tests
services/assistant/       Python package `assistant`, likewise
contracts/events/*.avsc   Avro schemas — the Kafka contract
contracts/proto/*.proto   gRPC contracts
frontend/                 demo client (gains an Assistant page in Week 5)
```

One repository, because the git history is graded as one artefact and the contracts must
change in the same commit as their producer. Each service is still independently built and
deployed: its own image, its own dependencies, its own settings.

Enforced by tests, as the module rules are today (`tests/meta/test_import_boundaries.py`):

- **No imports between `app`, `knowledge` and `assistant`**, in any direction.
- **Contracts are files, not shared code.** Each service generates its own bindings from
  `contracts/`. A shared Python models package would couple the services again through the
  back door.
- **A service's settings cannot point at another's store.** A meta-test asserts that
  `knowledge`'s and `assistant`'s `Settings` have no field for Part A's Postgres, Redis or
  Mongo, and compose gives each container only its own DSNs.
- **Contract compatibility is checked in CI:** Schema Registry compatibility for every
  `.avsc` (`BACKWARD_TRANSITIVE`), `buf breaking` for every `.proto`.
- **A Part B service's tests run with Part A absent:** Part A's REST API is stubbed from its
  OpenAPI document, and events come from schema-valid fixtures.

---

## 2. Which mechanism carries which work

PRD D-7 split the work between Temporal, Kafka and Celery. Three services add two more
mechanisms (REST and gRPC between services), and the user's framing — **Celery for push,
Kafka for pull** — sharpens the rule. Strictly, both pull (Celery workers consume from
RabbitMQ); the real difference is the *kind of message*:

| Mechanism | Carries | Recipient | Afterwards |
| --- | --- | --- | --- |
| **Temporal** | A multi-step business process that may need undoing | The workflow | Durable history |
| **Kafka** | A **fact**: "appointment X was cancelled" | Anyone, each at its own pace | Kept and replayable |
| **Celery** | A **command**: "send this SMS" | One worker, once | Gone |
| **REST / gRPC** | A question or action whose answer the caller needs now | The owning service | — |

The decision procedure, in order (extends ARCHITECTURE §3.1):

1. **Does partial failure need compensating?** Temporal.
2. **Does the caller need the answer to carry on?** A synchronous call to the owner.
3. **Does anyone other than the caller need to know?** Kafka.
4. **Otherwise** — a job that must not hold the request — Celery.

They compose: **Kafka says what happened, a consumer decides, Temporal or Celery does it.**

**D-20 — Kafka starts Temporal workflows; it never steers a running one.** A consumer of a
domain event starts a workflow under a **deterministic workflow id**, so redelivery of the
event is refused by Temporal (the D-11 property, reused). Kafka never carries signals into a
running workflow for human actions: D-12 already showed that a signal-driven visit lifecycle
loses check-ins. *Rejected:* request/reply over Kafka (a network hop and consumer lag on
work that was a function call), publishing from inside workflow code (not replay-safe).

**D-21 — Events leave a database through a transactional outbox, never by a direct
publish.** Every producer writes its event as a row in an `outbox` table **in the same
transaction** as the business change; a relay publishes rows to Kafka and marks them sent.
A business write and a Kafka publish cannot be atomic (the same class of problem as PRD
R-1); with the outbox, the event exists exactly when the change does. A workflow that needs
to emit an event does so through an activity that writes the outbox in its own transaction.
*Rejected:* publish after commit (lost on crash — PRD R-17 is this bug); Debezium CDC (a
Kafka Connect cluster and a replication slot for a single-node demo; the polling relay is
the same pattern with fewer moving parts and can be swapped later without changing a
consumer).

---

## 3. Event backbone (Week 3)

### 3.1 Envelope and schemas

**D-22 — Avro schemas in Schema Registry, `BACKWARD_TRANSITIVE` compatibility, one
subject per topic.** Avro is Schema Registry's native format, the compatibility rules are
the best understood, and `.avsc` files are JSON a reviewer can read. Backward-transitive
means a consumer on the newest schema can read every event ever written — required, because
compacted topics (§3.2) are replayed from offset 0 by new consumers.

Every event value carries:

| Field | Meaning |
| --- | --- |
| `event_id` | uuid7; the idempotency key every consumer dedupes on (harness requirement 4) |
| `event_type`, `schema_version` | e.g. `appointments.cancelled`, `1` |
| `occurred_at` | from `app/core/clock.py`, never `datetime.utcnow()` (harness requirement 6) |
| `producer` | `part-a`, `knowledge`, `assistant` |
| `subject_id` | the entity the event is about; also the **Kafka message key**, so all events for one entity land on one partition in order |
| the payload | fields per event (§3.2) |

Trace context travels in **Kafka headers** (`traceparent`, `tracestate`), not in the value
(§7.2).

**Events are "fat"**: they carry the state a consumer needs, so a consumer in another
service never has to call back for it. That is what lets `knowledge` build its own copy of
provider data (§3.2) and what makes Part B independent of Part A being up.

### 3.2 Topic catalogue

Two kinds of topic. **Business-event topics** are time-retained (7 days) logs of things that
happened. **State topics** are **log-compacted**, keyed by entity id: Kafka keeps at least
the latest value for every key forever, so a brand-new consumer reads from offset 0 and has
a complete copy of every entity. A deletion is a tombstone (a `null` value).

Every name is built through `Settings.topic()` (harness requirement 1). Topic
auto-creation is **turned off** (`KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`; compose sets it
`true` today): compaction and partition count must be declared, and a misspelt topic must
fail loudly. Topics are created by an idempotent `python -m app.cli kafka-topics` step that
compose runs before the services start.

| Topic | Kind | Producer | Consumers | Payload (beyond envelope) |
| --- | --- | --- | --- | --- |
| `appointments.booked` | event | Part A — `confirm_booking`'s transaction | analytics; notifications; `assistant` (conversion) | `appointment_id, patient_id, provider_id, clinic_id, department_id, slot_id, starts_at, ends_at, origin, conversation_ref` |
| `appointments.cancelled` | event | Part A — `cancel_appointment`'s transaction | cancel-follow-up starter (Temporal); notifications; analytics | `appointment_id, slot_id, patient_id, provider_id, starts_at, reason, initiated_by` |
| `appointments.rescheduled` | event | Part A — `complete_reschedule`'s transaction | notifications; analytics | `old_appointment_id, new_appointment_id, old_slot_id, new_slot_id, patient_id, new_starts_at` |
| `visits.completed` | event | Part A — `complete_visit`'s transaction | post-visit starter (Temporal); analytics | `visit_id, appointment_id, patient_id, provider_id, checked_in_at, seen_at, completed_at` |
| `appointments.no_show` | event | Part A — `mark_no_show`'s transaction | analytics; notifications | `appointment_id, patient_id, provider_id, starts_at` |
| `billing.status_updated` | event | Part A — billing activities | notifications; analytics | `appointment_id, patient_id, billing_status, amount_minor, currency` |
| `identity.sessions_revoked` | event | Part A — logout-all / deactivation | `assistant`, `knowledge` (deny-list) | `user_id, revoked_before` |
| `providers.state` | **state** | Part A — every provider create/update/deactivate | `knowledge`; deactivation starter (Temporal) | full provider: `name, specialty, bio, clinic_id, department_id, is_active, languages` |
| `clinics.state`, `departments.state` | **state** | Part A — `seed` today; endpoints later (FR-1.5) | `knowledge` | full record |
| `knowledge.document_indexed` | event | `knowledge` | analytics (Grafana) | `document_id, version, chunk_count, audience` |
| `assistant.interaction` | event | `assistant` | Part A analytics (optional) | `conversation_id, user_role, intent, answered, latency_ms, tokens` |
| `*.dlt` | dead letter | consumer framework | humans, via Grafana and the replay CLI | the original message plus `error`, `attempts`, `consumer` |

`providers.deactivated` (named in PRD R-25) is not a separate topic: it is the
`providers.state` value whose `is_active` became `false`. One topic per entity's state, and
consumers that care about the transition compare old and new.

**Backfill.** Rows that exist before the producer does (everything `seed` has written, and
every provider created in Weeks 1–2) were never published. `python -m app.cli republish
--entity providers|clinics|departments` writes one outbox row per current record. Repeating
it is harmless: compaction keeps the latest, and consumers dedupe.

`origin` (`web`, `front_desk`, `assistant`, `waitlist`) and the opaque `conversation_ref`
are new nullable columns on `appointments`, set from an optional request header. They are
what lets the assistant measure *booking conversion after AI interaction* (PART-B-FR-5)
without Part A knowing anything about conversations.

### 3.3 Outbox and relay

`outbox` (Part A's database; each Part B service has its own):

| Column | Note |
| --- | --- |
| `id` | uuid7 = the event's `event_id` |
| `topic`, `key`, `value` (bytea, Avro-encoded with the schema id), `headers` (jsonb) | `headers` stores the **`traceparent` of the transaction that wrote the row**, so the trace survives the hop through the table |
| `created_at`, `published_at` (null until sent), `attempts`, `last_error` | |

The relay is its own process (`python -m app.relay`, compose service `relay`), not a Celery
beat job — it is a tight loop, not a schedule:

1. `SELECT … WHERE published_at IS NULL ORDER BY id LIMIT 500 FOR UPDATE SKIP LOCKED`
2. produce each with an **idempotent producer** (`enable.idempotence=true`, `acks=all`)
3. on delivery report, set `published_at`; commit.

Order is per key, and `id` is time-ordered, so one entity's events leave in the order they
were written. A crash between produce and commit re-sends: at-least-once, which every
consumer already assumes. `SKIP LOCKED` lets a second relay replica run safely. Published
rows are deleted after 7 days by a beat job.

The one number that says whether the event system is healthy is
**`outbox_oldest_unpublished_age_seconds`** (§7.3); it has an alert.

### 3.4 Consumer framework

Each service has a small consumer runtime (no shared library — the shape is the same, the
code is each service's):

- **Registered.** Consumers register in a `Registry` (`app/core/registry.py` in Part A), and
  a meta-test discovers every consumer and fails if one is unregistered (harness
  requirement 3), as Temporal does today.
- **Idempotent by a table, not by Redis.** The handler's side effect and an
  `INSERT INTO processed_events (consumer, event_id)` commit **in the same transaction**; a
  duplicate hits the primary key and the handler is skipped. A Redis "seen" mark could
  succeed while the side effect's transaction fails, or the reverse. For handlers whose side
  effect is a Temporal start, the deterministic workflow id is the dedupe and the table
  records it for observability.
- **Offsets committed after the handler**, manually; never auto-commit.
- **Retry then dead-letter.** A failing message is retried in place with backoff (3
  attempts), then written to `<topic>.dlt` with the error and the offset is committed, so
  one poison message cannot stall a partition. `python -m app.cli dlt-replay --topic …`
  re-publishes dead letters after a fix.
- **Meta-test `test_consumers_idempotent.py`** (planned in ARCHITECTURE §3.5): for every
  registered consumer, delivering its fixture event twice yields one side effect.

### 3.5 Kafka → Temporal: three uses

| Event | Consumer starts | Workflow id | Closes |
| --- | --- | --- | --- |
| `appointments.cancelled` | `CancelFollowUpWorkflow` (exists: stop visit timer, refund, waitlist) | `cancel-followup-<appointment_id>` | **PRD R-17** — the follow-up can no longer be lost: the cancel's transaction writes the event, and the existing best-effort start in `followups.py` is removed |
| `providers.state` with `is_active` true → false | `ProviderDeactivationWorkflow` (new): list the provider's future live appointments, cancel each through the existing paired write with reason `provider_unavailable`, which in turn emits `appointments.cancelled` (notification, refund, waitlist) | `provider-deactivation-<provider_id>-<event_id>` | **PRD R-25** |
| `visits.completed` | `PostVisitWorkflow` (new): billing charge activity, then a durable timer, then a follow-up reminder enqueued to Celery | `post-visit-<visit_id>` | **PART-A-FR-3**'s billing and follow-up |

**D-23 — A deactivated provider's future appointments are cancelled and the patients
notified.** *Decided 2026-11-10.* The R-25 policy choice. *Rejected:* automatic reassignment to another provider
(a clinical choice — specialty, continuity, language — that should not be made by code);
refusing deactivation while appointments remain (blocks the admin in exactly the case —
sudden absence — when deactivation is urgent). The notification tells the patient the
waitlist and the assistant can find a new slot.

The deactivation workflow cancels through Part A's own service function, inside Part A — it
is Part A's workflow reacting to Part A's event. The event is used because the `providers`
module cannot import `scheduling` (D-8), not because a service boundary is crossed.

---

## 4. Redis

### 4.1 Part A: two instances, because eviction policies conflict

**D-24 — State and cache live in separate Redis instances.** Sessions, rate-limit counters
and locks must **never** be evicted (`maxmemory-policy noeviction`); caches **should** be
evicted under memory pressure (`allkeys-lru`). One instance has one policy, and
`volatile-lru` would evict sessions, because they have TTLs too.

| Instance | Use | Keys (all through `app/db/redis.py:key()`) |
| --- | --- | --- |
| `redis` (state, `noeviction`, AOF on) | sessions; refresh-token families; login throttling; API rate limits; Celery beat lock | `session:<sid>`, `user-sessions:<user_id>`, `refresh:<family>`, `throttle:login:<email>`, `rl:<route>:<user>` |
| `redis-cache` (`allkeys-lru`, no persistence) — *arrives with its first user, the slot cache (slice 5)* | `GET /slots` cache; Celery result backend for the few tasks that return a result | `slots:<provider>:<version>:<query-hash>`, `celery-result:*` |

### 4.2 Sessions — the case Week 1 deferred

Week 1 issued a 60-minute stateless HS256 JWT: no logout, and a deactivated user keeps
access until expiry (Week 1 API spec §11 named Redis as the fix).

**D-25 — Short asymmetric access tokens, server-side sessions in Redis, rotating refresh
tokens.**

- **Login** creates `session:<sid>` (user, role, created, last seen; TTL = session lifetime,
  12 h) and returns an **access token (10 min, EdDSA-signed, carries `sid`)** and a
  **refresh token** (opaque, rotating).
- **Each request** verifies the signature, then one Redis `EXISTS session:<sid>` — far
  cheaper than the per-request user query the Week 1 spec rejected.
- **Refresh** (`POST /auth/refresh`) rotates the refresh token; presenting an
  already-used refresh token revokes the **whole family** (a stolen token was replayed).
- **Logout** (`POST /auth/logout`) deletes the session. **Deactivating a user** deletes all of
  their sessions (`user-sessions:<user_id>`) and emits `identity.sessions_revoked`.
- **JWKS** (`GET /.well-known/jwks.json`) publishes the public key, so `knowledge` and
  `assistant` verify tokens **without holding Part A's secret** and without reading Part A's
  Redis. They keep a deny-list fed by `identity.sessions_revoked` in their own store, so a
  revocation reaches them in about a second; the access token's 10 minutes is only the bound
  if Kafka is down. Any *action* the assistant takes is a Part A API call, which checks the
  session itself — a revoked user can never act through the AI.
- **Login throttling:** a per-email and per-IP counter with backoff. It must keep the
  existing guarantee of no account enumeration (PART-A-NFR-8): a throttled login answers the
  same 401, in the same time, as a wrong password.

*As built (slice 2):* the demo frontend keeps both tokens in `sessionStorage`, and the
refresh token travels in request and response bodies. An `HttpOnly` cookie scoped to
`/auth/refresh` is the production choice and is deferred: it needs CSRF handling across
the Vite dev proxy. The client refreshes once on a 401, single-flight, because a refresh
token spent twice reads as a replay and ends the session.

### 4.3 Other Part A uses

- **API rate limits** on `POST /appointments` and `/reschedule` per user (peak booking
  windows, PART-A-NFR-5): fixed-window counters, answer 429 with `Retry-After`.
- **`GET /slots` cache.** Keyed by a per-provider version number that `scheduling/service.py`
  bumps (after commit) whenever it writes a slot, so a changed provider's entries simply stop
  being read. **The cache is never trusted for booking** — `claim_slot` stays the authority
  — so a stale entry costs one failed booking, never a double booking. Closes PRD R-7.
- **Notification throttling** inside the Celery send task: at most N messages per patient
  per hour.

### 4.4 Part B uses (`assistant-redis`)

| Use | Note |
| --- | --- |
| Conversation memory | LangGraph checkpoints of an active chat, with a TTL; the durable transcript is in `assistant-db` |
| Resumable streams | the answer's events written to a Redis Stream `turn:<turn_id>`; the gRPC `Chat` stream reads it, so a reconnect with `resume_from` replays from that point |
| LLM response cache | keyed on a hash of prompt + retrieved context + model; the same key serves the AI harness's record/fixture modes |
| Token budget and concurrency | per user and per provider, so a burst cannot exhaust the LLM quota |
| Session deny-list | from `identity.sessions_revoked` |

`knowledge` needs no Redis: its embedding cache is the content hash stored on each chunk
(§8.4).

**Where Redis is not used:** Kafka consumer dedupe (§3.4 — must be transactional with the
side effect); the vector index (plain Redis has no vector search; Qdrant is the vector DB).

---

## 5. Celery and RabbitMQ (Week 3)

### 5.1 Part A's `notifications` module

A new module, `app/modules/notifications/`, owning `notification_templates` and
`notifications` (one row per message: patient, channel, template, status, `idempotency_key`
unique, attempts, provider message id). Its Kafka consumers turn events into messages:

| Event | Message |
| --- | --- |
| `appointments.booked` | booking confirmation |
| `appointments.cancelled` | cancellation (and, for `provider_unavailable`, the rebook prompt) |
| `appointments.rescheduled` | new time |
| `appointments.no_show` | missed-appointment note with a rebook link |
| `billing.status_updated` | refund issued |
| reminder due (from Temporal, §5.2) | reminder |

The consumer writes the `notifications` row (status `queued`, idempotency key =
`event_id + channel`) and the `processed_events` row in one transaction, **then** enqueues
`notifications.send(notification_id)` — after commit, so a worker never finds no row. If the
enqueue fails, a beat sweep re-enqueues `queued` rows older than a minute; the task's first
step is to claim the row (`UPDATE … SET status='sending' WHERE status='queued'`), so a double
enqueue sends once.

**Delivery** goes through a channel adapter: `email` (SMTP to **Mailpit**, a compose
service with a web inbox, so a demo shows real messages arrive) and `sms` (a logging fake).
Task settings: `acks_late=True`, `task_reject_on_worker_lost=True`, exponential backoff with
jitter, 5 attempts, then the message goes to a RabbitMQ **dead-letter queue** and the row to
`failed`. Every task registers in a task registry and passes
`test_tasks_idempotent.py` (re-executing with the same key is a no-op).

**D-26 — Reminder timing lives in Temporal; Celery only delivers.** The `VisitWorkflow`
that already exists for every confirmed appointment gains a reminder timer (24 h before
`starts_at`, configurable) that, when it fires, runs an activity writing a reminder
`notifications` row and enqueuing the send. D-12 already says the workflow owns time, and a
cancel already stops `VisitWorkflow` — so a cancelled appointment's reminder dies with it,
with no extra code. The change is introduced under `workflow.patched(...)` so in-flight
visit workflows replay. *Rejected:* Celery `eta`/`countdown` for a message days away
(RabbitMQ's `consumer_timeout` and unacked messages held in worker memory make it fragile,
and revocation is unreliable); a Celery beat scan of a `scheduled_notifications` table
(sound, but a second owner of time with its own cancel path). This replaces the
`booking.schedule_reminders` stand-in, which now does nothing but log and is removed.

### 5.2 Beat schedule (Part A)

| Job | Every | Purpose |
| --- | --- | --- |
| `reconcile_stale_pending` | 5 min | Finds `pending` appointments older than 15 min whose booking workflow is closed or missing, and compensates them — closes **PRD R-9** (a terminated workflow strands a held slot) |
| `requeue_stuck_notifications` | 1 min | §5.1 |
| `purge_published_outbox` | daily | §3.3 |
| `analytics_reconcile` | nightly | §6 |

Beat runs as one container; a Redis lock in `redis` (state) guards against two beats if one
is ever scaled by mistake.

### 5.3 Billing

**D-27 — A `billing` module with `billing_records`, talking to a fake payment gateway.**
*Decided 2026-11-10.* The pre-check, the refund (`cancel.refund`) and the post-visit charge
stop being stand-ins that only log. Each is a Temporal activity that calls a **mock payment
gateway** and records the outcome as a `billing_records` row (appointment, kind
`precheck|charge|refund`, status, amount, gateway reference) through
`app/modules/billing/`, then emits `billing.status_updated`.

The gateway, `payments-mock`, is its own small compose service standing in for an external
provider — **it belongs to no service and shares no store** (D-19); Part A reaches it only
over HTTP:

- `POST /authorizations` (the pre-check: a hold), `POST /captures` (the charge),
  `POST /refunds`, `GET /transactions/{ref}`.
- **Every call carries an `Idempotency-Key`** — the appointment id and kind — and the mock
  honours it: a repeated key returns the first result instead of charging twice. This is
  what makes Temporal's activity retries safe against a real gateway.
- **Configurable misbehaviour** (environment variables, and a `POST /_chaos` endpoint for
  demos and tests): added latency, a failure rate returning 503, a rate of timeouts **after**
  the charge was taken (the hardest case — the activity times out, retries with the same key,
  and gets the original result), and a deterministic decline for a patient flagged
  `billing_hold`.
- The booking's compensation path now releases the authorisation hold (`POST
  /authorizations/{ref}/void`) as well as the slot.

The mock is written for this project (FastAPI, in-memory store with a file snapshot) under
`services/payments-mock/`, is traced and exports metrics like every other process, and has a
**circuit breaker** in front of it in Part A so a gateway outage fails bookings fast instead
of piling up retries. A real gateway stays a non-goal (PRD §2).

---

## 6. Analytics (Week 3, PART-A-FR-5)

The five metrics are **business data, not Prometheus metrics**: Prometheus counters reset
on restart and are not a system of record.

An `analytics` module in Part A consumes `appointments.*` and `visits.completed` into
daily rollup tables (`analytics_daily`: date, clinic, department, provider, booked,
cancelled, completed, no_show, wait_seconds_sum, wait_count) — idempotent through
`processed_events`, in one transaction per event. Total Patients is a count over `patients`.
The nightly `analytics_reconcile` recomputes the last 7 days from the source tables and
logs any drift, which is the check that the consumer has not lost or doubled an event.

`GET /analytics/summary` and `GET /analytics/timeseries?metric=…&from=…&to=…&group_by=…`
(admin, front desk) serve them; Grafana reads the same tables through a **read-only**
Postgres datasource for the "Platform analytics" dashboard.

| Metric | Source |
| --- | --- |
| Total Patients | `count(patients)` |
| Appointments Booked Over Time | `sum(booked)` by day |
| Completed Visits | `sum(completed)` |
| Cancellation Rate | `sum(cancelled) / sum(booked)` over the window |
| Average Wait Time | `sum(wait_seconds_sum) / sum(wait_count)` — `seen_at − checked_in_at` (PRD D-3) |

---

## 7. Observability (Week 3 first slice; every service thereafter)

### 7.1 Pipeline

```
every process ──OTLP──▶ OTel Collector ──┬─▶ Jaeger          (traces)
                                         └─▶ Prometheus      (metrics, scraped from the collector)
RabbitMQ, Kafka exporter, Postgres exporter ─▶ Prometheus
Grafana ◀── Prometheus, Jaeger, Part A analytics (Postgres, read-only)
```

**D-28 — Every process exports to one OTel Collector; nothing exports to a backend
directly.** A new process — including Part B's — joins by setting
`OTEL_EXPORTER_OTLP_ENDPOINT` and a `service.name`. The tracer provider is built in one
place per service and stays swappable (harness requirement 5) so tests install an in-memory
exporter. Grafana dashboards and datasources are **provisioned from files** in
`ops/grafana/`, so they are versioned and reviewable.

Processes and their `service.name`: `part-a-api`, `part-a-worker` (Temporal),
`part-a-relay`, `part-a-consumers`, `part-a-celery`, `part-a-beat`, `knowledge-api`,
`knowledge-ingest`, `assistant-api`, `assistant-worker`, `envoy`.

### 7.2 One trace, end to end

The deliverable "distributed tracing enabled" is demonstrated as **one Jaeger trace** for a
cancel:

```
POST /appointments/{id}/cancel                   FastAPI instrumentation
 └ cancel transaction + outbox row               SQLAlchemy; the row stores traceparent
   └ relay publish                                span linked to the stored context
     └ consumer: appointments.cancelled          confluent-kafka; context from headers
       └ start CancelFollowUpWorkflow            temporalio.contrib.opentelemetry.TracingInterceptor
         └ activity cancel.refund → billing row → outbox → billing.status_updated …
       └ consumer (notifications) → Celery send   Celery instrumentation; context in task headers
         └ SMTP to Mailpit
```

The only non-standard step is the outbox: the relay starts its span **from the
`traceparent` stored on the row**, otherwise every trace would break at the table. Logs are
the existing structured JSON with `trace_id` and `span_id` added, so a log line leads to its
trace. (No log backend is added; Loki is not in the mandated stack, and logs stay on
stdout.)

### 7.3 Metrics that matter

| Area | Metrics |
| --- | --- |
| HTTP | request rate, error rate, latency histogram by route (RED) |
| Temporal | SDK metrics (workflow and activity outcomes, schedule-to-start latency, task-queue backlog) |
| Outbox | **`outbox_oldest_unpublished_age_seconds`**, published per second, relay errors |
| Kafka | consumer lag per group and topic; DLT message count |
| Celery / RabbitMQ | task outcomes and duration by task; queue depth; DLQ depth (RabbitMQ's built-in Prometheus plugin) |
| Business | bookings confirmed / failed by reason; cancellations by reason; no-shows; notifications sent / failed by channel; rate-limit rejections; session revocations |
| Part B | retrieval latency, empty-retrieval rate, top-score distribution; ingestion lag (event time → searchable); LLM latency, errors and tokens by provider and model; time to first token; stream resumes |

### 7.4 Dashboards and alerts

Dashboards: **Booking flow**, **Async backbone** (outbox, Kafka lag, DLTs, Celery,
RabbitMQ), **Workers** (Temporal, Celery), **Platform analytics** (FR-5), **Knowledge**,
**Assistant**. Alert rules (Grafana-managed): outbox age > 60 s; any DLT or DLQ message;
consumer lag growing for 10 min; booking failure rate > 20 % over 15 min; LLM error rate.

---

## 8. `knowledge` — the RAG service (Week 4)

### 8.1 What RAG is for here, and what it is not for

RAG means: before the model answers, search our own documents for the passages closest in
meaning to the question and put them in the prompt, so the answer comes from our text and
can cite it. An **embedding** turns text into a list of numbers such that texts with similar
meaning have similar numbers; the **vector database** stores them and finds the nearest ones
fast.

**D-29 — The vector index holds shared knowledge only. Anything about one patient is
fetched by a tool call with that patient's token, never embedded.**

| Kind of knowledge | Examples | How |
| --- | --- | --- |
| Documents that rarely change | specialty catalogue (what each specialty treats), test preparation guides, FAQ, policies (late-cancel fee), clinic protocols (staff only) | **RAG** |
| Slowly changing records | provider profiles, departments, clinics | **RAG, kept fresh by `*.state` topics** |
| Live facts | free slots, *my* appointments | **Tool calls to Part A** (§9) — a slot index is stale in seconds, and "this week" is a date filter, not a meaning search |
| One patient's clinical record | diagnoses, prescriptions, ordered tests, uploaded reports (§9.6) | **Tool calls to Part A**, which owns the record and extracts report text. One patient's record is a handful of notes and a few reports — it fits in a prompt without search, and it never leaves Part A's authorisation |

If a patient's documents ever outgrow a prompt (years of reports), the answer is a
**patient-partitioned** index in `knowledge` whose patient filter, like the audience filter,
is taken from the token — not the shared index. That is deferred until a measured need.

The corpus is a small fictional MediNova document set written for the project and committed
under `services/knowledge/corpus/` (Markdown and PDF), plus the provider data arriving over
Kafka. Each document declares an **`audience`** (`patient`, `staff`).

### 8.2 Stack

| Choice | Why | Rejected |
| --- | --- | --- |
| **Qdrant** | A dedicated vector DB; payload filters (audience, clinic, active-only); **hybrid search** — meaning-based plus exact-term matching, which matters for terms like "HbA1c" or "MRI with contrast" that pure meaning search blurs | pgvector (fine, but hybrid search is hand-built), Redis (no vector search in plain Redis) |
| **`fastembed`**, local embeddings (a small BGE dense model + a BM25 sparse model) | No API key, no cost, the same result in CI; gives the sparse vectors hybrid search needs | A paid embeddings API — still possible behind the provider factory (harness requirement 2). Anthropic offers no embeddings API, so this choice is independent of the Week 5 LLM |
| **`pypdf`** for PDF text | Permissive licence | PyMuPDF (AGPL). Scanned PDFs needing OCR are out of scope |
| **LangChain** text splitters and document loaders | The mandated stack; heading-aware splitting is built in | — |

### 8.3 Ingestion

Two triggers, by the same push/pull rule as Part A:

- **Document upload (push).** `Knowledge.UploadDocument` (admin) stores the file and its
  version in `knowledge-db`, then enqueues a Celery `ingest_document` task.
- **Entity state (pull).** A consumer of `providers.state` / `clinics.state` /
  `departments.state` writes the record to `knowledge-db` and re-embeds it — one provider
  profile is one chunk. `is_active = false` or a tombstone removes it from the index.

The pipeline — **parse → split → embed → upsert** — is idempotent by construction:

- **Splitting** follows headings, about 300–500 tokens per chunk with a small overlap; every
  chunk carries `document_id, version, section, audience, clinic_id, content_hash`.
- **Chunk ids are deterministic** — a hash of document id, version and position — so a
  re-run overwrites instead of duplicating.
- **A chunk whose `content_hash` is unchanged is not re-embedded** (the embedding cache).
- **A new version is upserted before the old one is deleted**, so a search never sees a gap.

### 8.4 Retrieval

`Knowledge.Search(query, audience_from_token, filters, k)`:

1. Hybrid query (dense + sparse, fused) for the top 20, filtered by **the audience the
   caller's token allows** — the filter comes from the token's role, never from the
   caller's request, so a patient can never retrieve staff protocols even through a
   manipulated prompt.
2. Optional cross-encoder re-rank to the top 5.
3. A minimum-score threshold: below it, the result is empty, and the assistant says it
   does not know rather than guessing.
4. Return chunks with `document_id, section, score` for citation.

Query rewriting for vague questions is the assistant's job (it owns the LLM), not this
service's.

### 8.5 Measured, not eyeballed

A golden set of 30–50 questions, each with the chunks it should find, lives in
`services/knowledge/eval/`. CI computes **recall@5** and **MRR** offline in the AI harness's
`fixture` mode and fails below a stated bar. **Week 4 is done when `Search` returns relevant,
cited chunks and the golden set meets the bar** — no answer generation yet.

---

## 9. `assistant` — the AI service (Week 5)

### 9.1 Use cases by role

Every use case is one of three types: **RAG** (via `knowledge.Search`), **tools** (Part A's
REST API with the user's own token), or **generation** (a draft a human approves).

**Patient**

| Use case | Example | How |
| --- | --- | --- |
| Find the right specialist | "Chest tightness when I climb stairs — who should I see?" | RAG over the specialty catalogue and provider profiles. **Red-flag phrases get urgent-care guidance, never a booking** |
| Find and book | "Same cardiologist as last time, Thursday afternoon" | Tools: my history → provider → free slots → book, **after the user confirms** ("Book Dr X, Thu 15:30?") |
| Manage | "Move my Tuesday appointment to next week"; "put me on the waitlist" | Reschedule / cancel / waitlist tools, each confirmed |
| Prepare | "What do I do before my fasting blood test on Friday?" | RAG over prep guides, tied to the actual next appointment |
| My history | "What did the doctor say last time?" | Tool: my signed visit notes — the patient-facing summary and diagnosis (§9.6) |
| My prescriptions | "How should I take the antibiotic I was given?" | Tool: my active prescriptions. **The assistant restates the doctor's instructions word for word and never adjusts a dose**; anything beyond them is "ask your doctor or pharmacist" |
| Ordered tests | "What tests did my doctor ask for, and how do I prepare?" | Tool: my open test orders + RAG over the prep guide for each |
| Upload a result | "Here's my blood test report" | The upload is a Part A call (§9.6), made by the frontend; the assistant links it to the matching open order |
| Explain my report | "What does this result mean?" | Tool: the report's extracted text → plain-language explanation of what each value measures and the reference range printed on it, then "your doctor will review it". **Never a diagnosis**, and never before the ordering doctor has reviewed it if the clinic requires that (O-7) |
| Policies, FAQ | "Is there a fee if I cancel late?" | RAG |

**Front desk**

| Use case | Example | How |
| --- | --- | --- |
| Find capacity | "Who's free in dermatology at North this afternoon?" | Tools over slots and providers |
| Book a walk-in | "Book MRN 1042 with the first free GP" | Patient search → book with the front desk's token, confirmed |
| Phone queries | repetitive questions | RAG, the patient corpus |
| Draft messages | "Draft notices for Dr Y's patients — she's away next week" | Generation → staff edit and approve → Part A sends (§9.5) |
| Today's issues | "Who no-showed this morning, and who's waitlisted for those slots?" | Tools |
| Chase overdue tests | "Which patients have test orders older than two weeks with no result?" | Tools over order **status** only — front desk never sees diagnoses, prescriptions or results (§9.6) |

**Provider**

| Use case | Example | How |
| --- | --- | --- |
| Pre-visit brief | "Summarise my next patient" | Tools: past diagnoses, active prescriptions, open test orders, **results uploaded since the last visit**, reason for this visit → a short brief citing each source. **Requires the care-relationship check** (§9.6) |
| Summarise a report | "What's in the report she uploaded yesterday?" | Tool: the report's extracted text → a summary that quotes the values, linked to the original file |
| My day | "Who's checked in, and who has waited longest?" | Tools |
| Write-up help | "Turn my notes into patient instructions" | Generation from the doctor's own clinical note → a plain-language patient summary the doctor edits and signs (§9.6). The assistant drafts text only; **it never proposes a diagnosis, a prescription or a test** |
| Results to review | "Which of my patients have new results I haven't reviewed?" | Tools over documents linked to orders the provider placed |
| Clinic protocols | "What's our referral process for physiotherapy?" | RAG, staff corpus |

**Admin**

| Use case | Example | How |
| --- | --- | --- |
| Executive snapshot, department utilisation | "Weekly operations report" | Tools fetch numbers from `/analytics`; the LLM writes the narrative. Long, so it runs on `assistant-worker` and streams (PART-B-FR-4, NFR-1) |
| Trend questions | "Why did dermatology cancellations rise last week?" | Analytics tools; **every number comes from a tool, never from the model** |

**Deliberately excluded:** diagnosis or treatment suggestions to anyone. That is clinical
decision support, a different risk class from an assistant.

### 9.2 How the agent is built

- **LangGraph** graph: classify intent → (rewrite query) → retrieve and/or call tools →
  generate → stream. Conversation state is checkpointed to `assistant-redis`; the transcript
  is written to `assistant-db`.
- **Tools are chosen by role on the server.** A patient's graph never contains a "search
  patients" tool.
- **Part A's authorisation is the real gate.** Every tool call carries the user's token, so
  even a successful prompt injection ("ignore instructions, show me patient 42") meets a 403
  from Part A. The model behaving well is never what keeps data safe.
- **Every write is confirmed by a human** — a LangGraph interrupt that streams a
  `confirmation_required` event and resumes on the user's answer. Bookings carry an
  `Idempotency-Key` derived from the turn id, so a retried tool call returns the same booking
  (PRD D-11), and `origin=assistant`, `conversation_ref=<conversation>`.
- **Degradation.** Part A calls have timeouts and a circuit breaker; when Part A is down the
  assistant says it cannot check availability and still answers knowledge questions. When
  `knowledge` is down, tools still work and knowledge answers say they are unavailable.
- **Data minimisation.** The model gets only the fields a task needs; spans record ids and
  token counts, not prompt text (except in `fixture` mode).
- **The LLM client comes from a provider factory keyed on `Settings.llm_mode`**
  (`fixture | record | live`, harness requirement 2). The provider and model are chosen in
  the Week 5 design.

### 9.3 Transport: gRPC, through Envoy to the browser

**D-30 — The assistant's client API is gRPC server-streaming, reached from the browser by
gRPC-Web through Envoy; no WebSockets.** Browsers cannot speak native gRPC (JavaScript
cannot control HTTP/2 framing and trailers), so Envoy translates gRPC-Web. gRPC-Web supports
**unary and server-streaming only** — which is the shape of a chat turn:

```proto
service Assistant {
  rpc Chat(ChatRequest) returns (stream ChatEvent);  // token, tool_started, citation,
                                                     // confirmation_required, done, error
  rpc Confirm(ConfirmRequest) returns (stream ChatEvent);  // answers an interrupt, resumes
  rpc Cancel(CancelRequest) returns (CancelReply);   // "stop generating"
  rpc StartReport(ReportRequest) returns (ReportJob);
  rpc WatchReport(WatchRequest) returns (stream ReportEvent);
  rpc ListDrafts / ApproveDraft / RejectDraft …
}
service Knowledge {
  rpc Search(SearchRequest) returns (SearchReply);
  rpc UploadDocument(UploadRequest) returns (UploadReply);
}
```

- **Stop generating** is a separate unary `Cancel`, because the browser cannot send on an
  open stream; the server also stops the LLM call when the client disconnects, so an
  abandoned answer stops costing tokens.
- **Resume:** `ChatRequest.resume_from` replays from the Redis Stream (§4.4).
- **Auth:** the bearer token in gRPC metadata, verified against Part A's JWKS (§4.2). The
  assistant forwards the user's token to `knowledge`.
- **Why not WebSockets:** a typed protobuf contract with generated Python and TypeScript
  clients, built-in deadlines and cancellation, standard OTel gRPC instrumentation, and
  `buf breaking` in CI — the same compatibility discipline Schema Registry gives Kafka.
  WebSockets would win only for true two-way traffic mid-answer (voice, live interruption),
  which no use case needs.
- **Not gRPC:** the assistant's calls into Part A stay REST with the user's token, so Part
  A's authorisation lives in one place (its routers). Between `assistant` and `knowledge`,
  native gRPC with no Envoy.

**Envoy is the single front door:** `/api/*` → Part A, `/assistant.v1.Assistant/*` →
`assistant`, `/knowledge.v1.Knowledge/*` → `knowledge`. The Vite dev proxy points at Envoy.

### 9.4 Part B analytics (PART-B-FR-5), owned by the assistant

| Metric | Source |
| --- | --- |
| Assistant usage; questions asked / answered | `assistant-db` conversations and turns (answered = not empty-retrieval, not error) |
| Booking conversion after AI interaction | `appointments.booked` with `origin = assistant` and a `conversation_ref` it recognises, over conversations |
| Generated communication usage | drafts approved ÷ drafts generated |
| Average AI response time | turn latency and time to first token |
| Engagement improvement | returning users per week; share of bookings with `origin = assistant` over time |

### 9.5 Part A additions Part B needs

| Addition | When | Note |
| --- | --- | --- |
| `origin`, `conversation_ref` on appointments | Week 3 (with `appointments.booked`) | §3.2 |
| JWKS, sessions, `identity.sessions_revoked` | Week 3 | §4.2 |
| `/analytics` | Week 3 | §6 |
| **Clinical records** — notes, diagnoses, prescriptions, test orders, uploaded reports | Week 4 | §9.6 |
| **Care-relationship authorisation** (closes PRD R-14) | Week 4, with the clinical records | §9.6 — a provider reads a patient's record only if they have treated or are booked to treat them |
| **Send an approved notification** (`POST /notifications`, front desk and provider) | Week 5 | Approved drafts are sent by Part A's notifications module, so delivery, throttling and audit stay in one place |

### 9.6 Clinical records (Part A)

Today a visit ends with one click: `/complete` records a timestamp and nothing about what
happened. A real visit produces a **clinical record** — what the doctor found, what they
prescribed, what tests they want — and patients bring **reports** back. Every "previous
records" use case above reads this, so it is the system of record's job, not the
assistant's.

**D-31 — Clinical records are a Part A module, `app/modules/clinical/`, and its content
never travels over Kafka.** It is transactional patient data with the same consistency and
audit needs as appointments (PRD D-4, NFR-6). Events about it carry **ids and statuses
only**; anyone who needs the content reads it through Part A's API, under Part A's
authorisation. *Rejected:* fat clinical events (copies of diagnoses and prescriptions in a
retained log readable by any consumer group); storing records in Part B (the assistant
would become a system of record for clinical data).

**What it owns:**

| Table | Holds |
| --- | --- |
| `clinical_notes` | one per visit: presenting complaint, findings, **diagnoses** (free text plus an optional ICD-10 code, O-6), clinician-only notes, a **patient-facing summary**; status `draft → signed`, `signed_at`, `signed_by`; an amendment after signing is a new version, never an overwrite |
| `prescriptions` | per note: medication, strength, dose, route, frequency, duration, quantity, instructions; status `active | completed | stopped` |
| `test_orders` | per note: test from a **test catalogue** (which also links to its prep guide in the `knowledge` corpus), reason, urgency; status `ordered → result_uploaded → reviewed` |
| `patient_documents` | an uploaded report: who uploaded it, file in MinIO (`part-a-documents`), media type, size, sha256, optional `test_order_id`, extraction status, extracted text |

**The visit flow becomes:** check in → seen → the provider writes the note (a draft, saved
as they go) with any diagnoses, prescriptions and orders → **signs it** → `/complete`.
**Completing a visit requires a signed note** (*decided 2026-11-10*) (enforced in `complete_visit`, under the same
appointment lock), so a completed visit always has its record. A signed note can be amended
by its author as a new version, audited (O-3).

*As built (Week 4 slice 2):* the files live in a Docker volume behind the `DocumentStore`
interface (`app/modules/clinical/storage.py`), not MinIO — MinIO's images could not be pulled
from Docker Hub or Quay on 2026-11-10; an S3 adapter is one class. The upload body is the raw
file, not multipart. Diagnoses live inside the note version (JSONB); prescriptions and orders
are rows created on signing.

**Uploads.** `POST /patients/{id}/documents` (multipart; the patient for themselves, front
desk and the treating provider on their behalf) stores the file in MinIO and the row in one
step — the row is written last, so a failed upload leaves an orphan object for a sweep, never
a row pointing at nothing. Accepted: PDF and images, size-capped, type checked by content
not extension. Text extraction is a Celery job in Part A (`pypdf` for text PDFs; images and
scanned PDFs are stored and shown but not extracted — OCR is out of scope). Linking a
document to an open order moves the order to `result_uploaded` and notifies the ordering
provider.

**Who may read what** — role **and** relationship, checked in Part A's service layer:

| | Note: diagnoses, summary | Clinician-only notes | Prescriptions | Orders (status) | Documents / results |
| --- | --- | --- | --- | --- | --- |
| Patient (own record) | after signing | no | yes | yes | yes |
| Provider **with a care relationship** | yes | yes | yes | yes | yes |
| Provider without one | no | no | no | no | no |
| Front desk | no | no | no | status only | upload only |
| Admin | no (audit trail only) | no | no | no | no |

A **care relationship** means the provider has a completed visit with the patient, or a
live appointment with them. This is what closes **PRD R-14** — the gap where any provider
could act on any appointment — and the `seen`/`complete` endpoints move to the same check.
`clinical` reads appointment data through a published `scheduling.public` interface (D-15
pattern), never the tables.

**Events (ids only):** `clinical.note_signed` (`note_id, visit_id, patient_id, provider_id,
prescription_count, order_count`), `clinical.test_ordered`, `documents.uploaded`
(`document_id, patient_id, test_order_id`). Consumers: notifications ("your prescription and
instructions are ready", "please book or upload your test", "a new result for your
patient"), analytics (orders open, turnaround from order to result), and the post-visit
workflow, which uses `clinical.note_signed` to schedule a reminder for each open order.

**What the assistant may do with it:** read through the tools above, explain, summarise
and draft. It **never** creates or changes a diagnosis, prescription or order — those are
written only by a provider, in Part A, and signed.

---

## 10. Testing

The existing tiers apply to every service; the cheapest tier that proves the property wins.

| Property | Tier |
| --- | --- |
| A cancel writes its outbox row in the same transaction (rollback leaves neither) | integration |
| The relay publishes in key order and re-sends after a crash before commit | integration |
| Every consumer, given its event twice, has one side effect | meta (registry-driven) |
| A poison message lands in the DLT and the partition moves on | integration |
| `appointments.cancelled` starts exactly one `CancelFollowUpWorkflow` under redelivery | workflow + integration |
| Provider deactivation cancels every future live appointment, once | workflow |
| The reminder timer fires before the appointment and dies with a cancel | workflow (time-skipping) |
| A Celery task re-executed with the same key sends once | meta (registry-driven) |
| A revoked session is refused by Part A at once and by `assistant` after the event | integration |
| A throttled login is indistinguishable from a wrong password | contract |
| A trace spans API → outbox → Kafka → Temporal → Celery (in-memory exporter) | integration |
| Schema and proto changes are compatible | CI (`schema-registry` check, `buf breaking`) |
| No service imports another or holds another's DSN | meta |
| Retrieval meets the recall bar | knowledge eval (fixture mode) |
| A patient token never retrieves a `staff` chunk | knowledge integration |
| A visit cannot complete without a signed note | integration |
| A provider with no care relationship gets 404 for a patient's record; front desk sees order status but no diagnosis, prescription or result | integration + contract |
| No clinical content appears in any Kafka event (schema check over the `clinical.*` and `documents.*` subjects) | meta |
| An upload whose row write fails leaves no row, and the sweep removes its object | integration |

New core paths (`tests/core-paths.txt`) for the `Stop` hook: `services/*/src/**`,
`contracts/**`.

---

## 11. Delivery plan

**Week 3** — each slice a branch, a detailed design where it needs one, and validated cases.

| # | Slice | Closes |
| --- | --- | --- |
| 1 | **Observability base** — **built** — Collector, Jaeger, Prometheus, Grafana (provisioned); OTel in API and Temporal worker; trace ids in logs | PART-A-OBS-2/3 (base) |
| 2 | **Sessions** — EdDSA + JWKS, Redis sessions, refresh rotation, logout, revoke-on-deactivate, login throttling, rate limits; `redis` configured as the state instance (`redis-cache` moved to slice 5) | Week 1 §11 deferral (PRD R-26), R-7 (part) — **built** |
| 3 | **Event backbone** — outbox, relay, Schema Registry, `contracts/events/`, topic creation, consumer runtime with dedupe and DLT, all producers, `republish` | PART-A-FR-4, NFR-3 — **built**; as built, the outbox stores JSON validated against the Avro schema at write time (not Avro bytes), `occurred_at` is the row's insert time, and `identity.sessions_revoked` is emitted on deactivation only (a single logout stays bounded by the 10-minute token in Part B) |
| 4 | **Kafka → Temporal** — cancel follow-up via event, `ProviderDeactivationWorkflow`, `PostVisitWorkflow`; `billing` module and `payments-mock` | R-17, R-25, FR-3 — **built**; as built, billing is one state-machine row per appointment (not one row per operation), the consumer runtime retries an outage without limit instead of dead-lettering it, and `PostVisitWorkflow` only captures until slice 5 adds the follow-up |
| 5 | **Notifications** — Celery app on RabbitMQ, `notifications` module, Mailpit, reminder timer in `VisitWorkflow`, beat jobs (incl. stale-pending reconciler), `/slots` cache | R-9, R-7, the reminder stand-in — **built** except the `/slots` cache and `redis-cache`, deferred (nothing measured needs them yet; R-7 stays partly open); as built, one consumer (`notifications.patient-messages`) covers every patient-facing event, test runs share the `part-a` vhost and are separated by queue prefix |
| 6 | **Analytics** — rollup consumer, `/analytics`, Platform analytics dashboard, the full dashboard and alert set | PART-A-FR-5, OBS-3 — **built**; as built, the rollup is keyed by (day, clinic, provider) without department, the reconcile reports rather than corrects, and `appointments.cancelled` / `.no_show` gained an optional `clinic_id` (schema v2) |
| 7 | **Part B skeletons** — `knowledge` and `assistant` running in compose with their own stores; `contracts/proto/`; Envoy; JWKS verification; `knowledge` consuming `providers.state` into Qdrant; `assistant` streaming an echo through Envoy to the frontend while calling `knowledge.Search`; one trace browser → Envoy → assistant → knowledge | Fixes the Weeks 4–5 architecture before they start — **built**; as built, the browser path is proven by `scripts/grpcweb_smoke.py` (raw gRPC-Web through Envoy) rather than a frontend page, which joins Week 5 with the agent; `knowledge` creates its skeleton table at start-up (migrations arrive with Week 4's schema); bindings are generated per service (`scripts/gen_proto.py`) as namespace packages |

**Week 4** — `knowledge`: corpus, ingestion (upload + state topics), hybrid retrieval with
audience filtering, golden-set evaluation, Knowledge dashboard. *Slice 1 built:* ingestion
from the committed corpus and from `providers.state`, hybrid retrieval, the golden set
(recall@5 0.97, MRR 0.94); the upload RPC with its Celery worker and the Knowledge dashboard
are deferred — nothing uploads documents yet, and the pipeline they would call is this one. Part A, in parallel: the
`clinical` module (notes, diagnoses, prescriptions, test orders, uploads to MinIO, text
extraction), the signed-note requirement on `/complete`, care-relationship authorisation
(R-14), and the clinical screens in the demo client. The execution guidelines' Week 4
"data parsing (PDF)" is done twice, by design: corpus PDFs in `knowledge`, patient reports in
Part A — the same library, no shared code (§1.2).

**Week 5** — `assistant`: the LangGraph agent, role toolsets, confirmations, streaming with
resume and cancel, drafts and approval, streamed reports, Part B analytics, Assistant
dashboard; the frontend Assistant page. **Stretch:** engagement campaigns; structured
extraction of lab values from reports (so a provider can ask for a trend across years).

---

## 12. Open questions, with the default this design assumes

| # | Question | Default |
| --- | --- | --- |
| O-1 | Reminder lead time(s) | one reminder, 24 h before; configurable |
| O-2 | Billing pre-check rule | a patient flagged `billing_hold` fails; amounts from a per-department price table |
| O-3 | Amending a signed note | its author may amend it as a new version at any time, audited; the earlier version stays readable |
| ~~O-6~~ | Diagnosis coding | **Decided 2026-11-10:** free text required, ICD-10 code optional, from a small seeded code list |
| ~~O-7~~ | Can a patient see a result before the ordering doctor reviews it? | **Decided 2026-11-10:** yes — it is their own upload — and the assistant may explain it, marking it not yet `reviewed` and always ending with "your doctor will review it" |
| ~~O-8~~ | Notification channels | **Decided 2026-11-10:** email over SMTP to Mailpit, SMS through a logging fake |
| ~~O-9~~ | LLM API key | **Decided 2026-11-10:** the project owner supplies it when Week 5 starts; until then nothing may need `record` or `live` mode — `fixture` mode and local embeddings only |
| O-4 | Week 5 LLM provider and model | **decided in the Week 5 design** (confirmed 2026-11-10), behind the factory |
| O-5 | Re-ranking | off by default; turned on if the golden set shows it earns its latency |

## 13. Risks this design accepts

- **More moving parts.** Kafka, Schema Registry, RabbitMQ, two Redis, three Postgres,
  Qdrant, MinIO, Envoy, Mailpit, `payments-mock` and the observability stack is a heavy compose file. Mitigated by
  compose profiles (`infra`, `obs`, `partb`) so a Part A test run starts only what it needs.
- **Eventual consistency is now visible.** A notification, an analytics number or a
  re-indexed provider lags its cause by the outbox and consumer latency — seconds, and
  measured (§7.3). Nothing user-facing reads its own write through Kafka.
- **A single Kafka broker and a single RabbitMQ node.** Fine for a demo; replication factor
  1 is stated, not hidden.
- **Clinical data raises the stakes of every authorisation bug.** Mitigated by checking role
  and care relationship in the service layer (not only in routers), by keeping clinical
  content off Kafka (D-31), and by audit entries on every read of a record, not only writes.
- **Part B's copy of provider data can be stale** by the consumer lag; acceptable for
  descriptions and specialties, and live facts never come from it (D-29).
