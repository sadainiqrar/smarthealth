# SmartHealth — Product Requirements Document

**Product:** SmartHealth — Intelligent Healthcare Operations & Patient Engagement Platform
**Customer:** MediNova (hospitals, clinics, diagnostic centres, telemedicine providers)
**Status:** Week 1 of 5 complete; Week 2 in progress (slice 2 of 5, the booking workflow, built) · Part A in progress
**Last verified against the codebase:** 2026-10-28

> Requirement ids defined here are the anchors used by the test catalog
> (`tests/cases/*.yaml`) and by the generated coverage report at
> `tests/reports/traceability.md`. Changing an id here breaks that link.
>
> The authoritative source for scope is `docs/requirements/*.docx`. This document
> interprets those requirements into numbered, traceable items; where the two disagree,
> the `.docx` wins.

---

## 1. Problem

MediNova's growth has outpaced its systems. Patient records, appointments, billing and
clinical operations live in disconnected systems; scheduling is slow and partly manual;
peak booking windows create latency; notifications are unreliable; and operational data
across clinics is inconsistent.

The engineering problem underneath all of that is **partial failure**. Booking one
appointment touches six subsystems — slot reservation, conflict checking, calendar sync,
billing pre-check, notification scheduling, analytics — and today any of them can succeed
while the others fail. The result is a confirmed booking billing never saw, or a slot
held by an appointment that no longer exists.

**SmartHealth is the backend that makes those multi-step healthcare operations reliable.**

## 2. Goals and non-goals

### Goals

1. A patient and provider management system staff can operate confidently, with an audit
   trail over every operational change.
2. A scheduling backbone where an appointment reaches `confirmed` **only** after every
   step of the booking workflow has succeeded, and where partial failure leaves no
   corrupted state.
3. Event-driven background processing that never blocks a user-facing request and never
   double-processes on retry.
4. Operational visibility sufficient to diagnose a failure in any critical flow.
5. (Part B) A GenAI layer that helps patients self-serve and staff generate communication,
   without blocking core healthcare operations.

### Non-goals

- Clinical decision support or diagnosis. The AI assistant routes to a provider; it does
  not practise medicine.
- A patient- or staff-facing UI. This is a backend. `frontend/` is a demo client built to
  show it (`docs/superpowers/specs/2026-10-28-frontend-demo-client-design.md`); it is not a
  requirement and nothing in the backend depends on it.
- Payment processing. Billing is modelled as a pre-check and status, not a payment gateway.
- Multi-tenancy across healthcare networks. Multi-clinic within one network is in scope.

## 3. Actors

| Actor | Needs |
| --- | --- |
| **Patient** | Book, reschedule and cancel appointments; receive reminders; view booking history; (Part B) ask questions and get guidance |
| **Provider** | Manage schedule and consultation slots; see their appointments; record visit progress |
| **Front desk staff** | Register walk-in patients, manage appointments, maintain department availability |
| **Admin** | Register providers, configure clinics and departments, view operational analytics |

A **patient is not a user account.** Front desk staff register walk-ins who have no
credentials; a login may be linked later. See §7 D-1.

## 4. Key use cases

| # | Use case | Actor | Primary requirement |
| --- | --- | --- | --- |
| UC-1 | Register a walk-in patient who has no login | Front desk | `PART-A-FR-1` |
| UC-2 | Register a provider and their specialty | Admin | `PART-A-FR-1` |
| UC-3 | Authenticate and receive a role-scoped token | All | `PART-A-FR-1` |
| UC-4 | Book an appointment, confirmed only on full success | Patient, Front desk | `PART-A-FR-2` |
| UC-5 | Cancel or reschedule, releasing the slot and moving the waitlist | Patient, Front desk | `PART-A-FR-2` |
| UC-6 | Check a patient in and record the visit through to completion | Provider, Front desk | `PART-A-FR-3` |
| UC-7 | Deliver a reminder without blocking the booking request | System | `PART-A-FR-4` |
| UC-8 | Report operational metrics across clinics | Admin | `PART-A-FR-5` |
| UC-9 | Diagnose why one booking failed | Engineer | `PART-A-OBS-2` |
| UC-10 | Ask "which specialist should I see?" and get a grounded answer | Patient | `PART-B-FR-2` |
| UC-11 | Generate a follow-up communication draft | Staff | `PART-B-FR-3` |
| UC-12 | Produce a department utilisation summary | Admin | `PART-B-FR-4` |

## 5. Functional requirements — Part A

| Id | Requirement | Milestone | Status |
| --- | --- | --- | --- |
| **PART-A-FR-1** | **Patient & user management** — patient registration and profile management; provider registration and specialty management; users with roles `patient`/`provider`/`front_desk`/`admin`; clinic and department management; audit trail over profile and operational changes | Week 1 | **Partial** |
| `PART-A-FR-1.1` | Patient registration and profile management | Week 1 | Done |
| `PART-A-FR-1.2` | Provider registration and specialty management | Week 1 | Done |
| `PART-A-FR-1.3` | Role-based access control across all endpoints | Week 1 | Done |
| `PART-A-FR-1.4` | User account creation | Week 1 | Partial — CLI only, no endpoint |
| `PART-A-FR-1.5` | Clinic and department management | Week 2 | **Not started** — schema only |
| `PART-A-FR-1.6` | Audit trail with actor, action, before/after | Week 1 | Done |
| **PART-A-FR-2** | **Appointment scheduling workflow** — validate patient eligibility, reserve the provider slot, prevent conflicts, schedule notifications, initiate billing pre-check, and mark the appointment `confirmed` **only after** all of it succeeds. Partial failure must not corrupt scheduling state. Cancellation and reschedule are first-class flows triggering slot release, waitlist movement, billing updates and notifications | Week 2 | **In progress** — booking built (Week 2 slice 2): `POST /appointments` starts `BookAppointmentWorkflow`, which confirms last and compensates on any failure; `GET /appointments/{id}` reads the result. Billing pre-check and reminder scheduling are stand-in activities until Week 3. Cancel/reschedule and the waitlist land in slice 4 |
| **PART-A-FR-3** | **Visit & service workflow** — check-in recorded, visit progress updated, completion stored, billing and follow-up triggered. Must be idempotent under retry, prevent duplicates, and recover from mid-flight failure | Week 2 | **Partly built (Week 2 slice 3)** — the visit lifecycle: `POST /appointments/{id}/check-in`, `/seen` and `/complete` write the visit synchronously, in the request's transaction and under the appointment row lock, and answer 200 (409 on an illegal step, repeats included); `GET /appointments/{id}/visit` reads it. A durable `VisitWorkflow`, started when a booking confirms, owns only the no-show timer (D-12). **Billing and follow-up on completion are Week 3 — none was added**; completing a visit changes the visit and appointment rows and nothing else |
| **PART-A-FR-4** | **Distributed & event-driven behaviour** — domain events (appointment booked, cancelled/rescheduled, schedule changed, reminder due, billing updated, visit completed) processed independently of user-facing flows, traceable, recoverable, without double-processing | Week 3 | **Not started** |
| **PART-A-FR-5** | **Analytics** — Total Patients; Appointments Booked Over Time; Completed Visits; Cancellation Rate; Average Wait Time | Week 3 | **Not started** — schema supports all five |

### Observability & reliability — Part A

| Id | Requirement | Milestone | Status |
| --- | --- | --- | --- |
| **PART-A-OBS-1** | Liveness and readiness endpoints; structured logging; a uniform error contract that leaks no internal detail | Week 1 | **Done.** Structured logging is verified at the *process* level by `sys-006`, not only at the formatter — uvicorn's `propagate = False` meant the served application emitted no JSON at all until that was fixed. The error contract became genuinely uniform when R-6 closed |
| **PART-A-OBS-2** | Distributed tracing across booking, availability sync, billing, reminders and background workers | Week 3 | Not started |
| **PART-A-OBS-3** | Metrics and dashboards (Prometheus + Grafana) | Week 3 | Not started |
| **PART-A-OBS-4** | Clear separation of responsibilities between modules, so a failure is attributable to one | Week 1 → ongoing | Done for Week 1 scope |

## 6. Functional requirements — Part B

| Id | Requirement | Milestone | Status |
| --- | --- | --- | --- |
| **PART-B-FR-1** | **Ingestion & indexing** — operational and document data parsed (incl. PDF), chunked, embedded, and stored as searchable vector records when source data changes | Week 4 | Not started |
| **PART-B-FR-2** | **Contextual patient Q&A** — retrieve relevant service, provider and operational context; answer vague or incomplete queries; ground every clinical claim in retrieved content | Week 5 | Not started |
| **PART-B-FR-3** | **Engagement & recommendation** — generate reminders, follow-up guidance, service recommendations and preventive-care suggestions | Week 5 | Not started |
| **PART-B-FR-4** | **Report & summary generation** — daily appointment summaries, department utilisation, engagement summaries, executive snapshots; long responses delivered incrementally | Week 5 | Not started |
| **PART-B-FR-5** | **Analytics** — assistant usage; questions asked/answered; booking conversion after AI interaction; generated-communication usage; average AI response time | Week 5 | Not started |
| **PART-B-OBS-1** | Diagnosable AI assistant interactions, retrieval pipeline, streaming responses and external LLM provider calls | Week 5 | Not started |

## 7. Non-functional requirements

| Id | Requirement | Verified by |
| --- | --- | --- |
| **PART-A-NFR-1** | **Correctness under concurrency.** Two simultaneous bookings for one slot must not both succeed. Enforced by the database, not by application logic | The conditional claim in `scheduling/service.py` `claim_slot` (D-2), backed by the partial unique index `ux_appointments_live_slot`; T3 `bkg-003` (a claim blocked behind a winner loses on the `status = 'free'` predicate) and `test_two_bookings_for_one_slot_confirm_exactly_one` |
| **PART-A-NFR-2** | **No corrupt scheduling state after partial failure.** A workflow that fails midway leaves no orphaned slot or half-confirmed appointment | T2 `bkg-002` (every failing booking step compensates and never confirms); the paired compensation write by T3 `test_fail_booking_releases_its_held_slot` |
| **PART-A-NFR-3** | **Idempotency.** All asynchronous consumers assume at-least-once delivery and take an explicit idempotency key | Registry-driven meta-tests (Week 3) |
| **PART-A-NFR-4** | **Background work never blocks a user-facing request** | Week 3 |
| **PART-A-NFR-5** | **Scale.** Tens of thousands of appointment requests; concurrent schedule updates; peak booking windows; multi-clinic operation | Week 3 load exercise |
| **PART-A-NFR-6** | **Data consistency.** PostgreSQL is the single system of record for all transactional state; MongoDB owns only the append-only audit trail | Schema constraints; §8 D-4 |
| **PART-A-NFR-7** | **Auditability.** Every profile and operational mutation records actor, action, and before/after state | Awaited audit write; T3 tests |
| **PART-A-NFR-8** | **Security.** Role-gated writes; argon2 password hashing; no account enumeration via message *or* timing; no internal detail in error responses | T1 contract tests |
| **PART-B-NFR-1** | **Long AI responses stream or run asynchronously** and must not hold a request or block core operations | Week 5 |
| **PART-B-NFR-2** | **Determinism in test.** LLM and embedding behaviour is reproducible offline via `fixture`/`record`/`live` modes | Harness spec §9 (Week 4–5) |

## 8. Key design decisions

Full rationale for D-1 … D-8 in `docs/superpowers/specs/2026-09-02-week1-foundation-design.md`;
for D-9 onwards in `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §2.
D-12 (the visit lifecycle) is added here now that slice 3 has landed. D-13 and D-14
(waitlist promotion, reschedule ordering) are decided in that spec and are added here when
the slice that builds them lands.

| Id | Decision | Rejected alternative | Why |
| --- | --- | --- | --- |
| **D-1** | A user account is distinct from a patient/provider record, linked optionally | One `users` table with a role column | Front desk must register walk-ins with no email and no password; the obvious design forces a fake login |
| **D-2** | Bookable time is a pre-generated `provider_slots` row; booking is a conditional `UPDATE … WHERE status='free'` | Compute availability from working hours on request | Two concurrent requests both compute "free" and both book. With rows, zero rows updated **is** the conflict signal — no window between checking and acting |
| **D-3** | An appointment (the booking) is separate from a visit (what happened) | One table with booked/arrived/seen/completed columns | Different lifecycles updated by different workflows. Split, Average Wait Time is `seen_at − checked_in_at` and a no-show is an appointment with no visit |
| **D-4** | PostgreSQL is the system of record; MongoDB owns the audit trail only | Audit rows in PostgreSQL (incl. JSONB) | Audit data is append-only, shape-varying per entity, write-heavy and unbounded. **Accepted cost:** the audit write and the business commit are not atomic — see §9 |
| **D-5** | Reliability invariants are enforced by database constraints, not application code | Validate in the service layer | A constraint binds every writer forever, including future code paths and manual intervention |
| **D-6** | Services never import FastAPI; routers own the transaction boundary | Services raise `HTTPException` and commit | Week 2's Temporal activities call the same functions with no HTTP request, and need to distinguish retryable from permanent failure — which a status code cannot express |
| **D-7** | Temporal owns durable multi-step workflows; Kafka carries domain events; Celery/RabbitMQ runs fire-and-forget jobs | Use one of the three for everything | Reaching for the wrong one is the most likely architectural error in this project |
| **D-8** | A modular monolith: one deployable, one schema, with module boundaries enforced by meta-tests | Microservices per domain | The assignment says "services/**modules**" (`part-a-core-platform.md:112`) — separation of *responsibilities* is graded, separation of *processes* is not. The headline invariants are PostgreSQL constraints (D-5) and constraints do not cross a service boundary: splitting scheduling would convert a GiST exclusion constraint, a partial unique index and composite foreign keys into distributed sagas. **Accepted cost:** blast radius and all-or-nothing API scaling — see R-8 |
| **D-9** | `ProviderSlot` moves from `providers` into `scheduling` (`app/modules/scheduling/models.py`) | (a) `providers` publishes claim/release functions, one allowed edge; (b) scheduling writes raw SQL on `provider_slots` | Slots are bookable inventory and scheduling is the only module that mutates them. The paired write (appointment + slot in one transaction) stays inside one module, and the horizontal import rule needs no exception for it. (a) makes the paired write straddle a boundary; (b) passes the import test while coupling to another module's table behind its back. The table, constraints and migrations are untouched — only the Python class moved |
| **D-10** | The booking workflow creates the `pending` row; the router only starts the workflow | Router inserts the row, then starts the workflow | Two systems cannot be written atomically. Workflow-first means a failed start leaves nothing behind, and a started workflow either confirms or compensates to `failed`. Router-first leaves a permanently `pending` orphan whenever the start fails, which needs a reconciler. Workflow-first needs none — **unless a workflow is terminated**: termination runs no workflow code, so compensation is skipped. The operational rule is "cancel, never terminate", and a reconciler for stale `pending` rows is deferred (R-9) |
| **D-11** | Duplicate submission is prevented by deterministic workflow ids: `Idempotency-Key` → `appointment_id` (a `uuid5` of caller and key) → `booking-<appointment_id>`, started with `USE_EXISTING` + `REJECT_DUPLICATE` | Dedupe table keyed on request hash | Temporal already refuses a second start under one workflow id; a second store would be a second source of truth for the same fact. A key reused with a *different* request is a 422, detected by a request fingerprint in the workflow memo (`start_booking`, `app/modules/scheduling/booking.py`) |
| **D-12** | *Amended 2026-10-29.* **The database records human actions; the workflow owns time.** Check-in, seen and complete are written synchronously by their endpoints, in the request's transaction, under the appointment row lock, and answer 200. One `VisitWorkflow` per confirmed appointment is only the durable no-show timer; at the deadline it asks the database, under the same lock, whether anyone checked in | (a) *The original D-12:* a long-running workflow driven by staff signals, writing each action through an activity after a 202 — a review proved it loses check-ins (Week 2 spec §4.2); (b) Temporal Updates, so the endpoint waits for the workflow's write — considered and not chosen: the write still lives behind the workflow, every action then depends on Temporal being up, and the no-show race still has to be settled in the database; (c) no-show by periodic job — needs a scheduler, and Temporal is one | Each human action is recorded the moment staff are told it was, so a timer can never overrule it, and a Temporal outage cannot fail one. The timer keeps what Temporal is uniquely good at: a durable per-appointment deadline that survives restarts, with an inspectable history |
| **D-15** | `patients` exposes a published interface, `app/modules/patients/public.py` (`ensure_bookable_patient`, `patient_id_for_user`); scheduling imports that file and nothing else from `patients` | Scheduling queries the `patients` table directly | A read-only query against another module's table is still coupling to its schema. Two declared edges onto one file — from `scheduling.activities` and `scheduling.router` — are visible in a diff and in `CROSS_MODULE_ALLOWED`. `scheduling/service.py` takes none: `NEVER_CROSS` forbids it (D-8) |

## 9. Known gaps and accepted risks

| Id | Gap | Status |
| --- | --- | --- |
| **R-1** | The audit write (MongoDB) and the business commit (PostgreSQL) are not atomic. If the commit fails after the audit write, MongoDB records a change that never took effect | **Accepted** for Week 1. The chosen ordering makes the *detectable* failure the likely one; the reverse would silently lose the record entirely |
| **R-2** | *Restated 2026-10-28* (Week 2 spec §2). The Week 1 wording — "cancel, reschedule, no-show and completion must free the slot" — was wrong for two of the four: a completed or no-show slot is in the past and must not return to `free`. The invariant is **slot status agrees with appointment status, written in one transaction**: `confirmed` → slot `booked`; `failed` (compensation) → `free` if this appointment held it; `cancelled` / `rescheduled` → `free`; `completed` / `no_show` → stays `booked` | **Booking, compensation and visit paths done; cancel/reschedule pending** (slice 4). `confirm_booking` (`held → booked`) and `fail_booking` (`held → free`) in `app/modules/scheduling/service.py` write the slot in the appointment's transaction (T3 `test_confirm_books_the_slot_and_is_idempotent`, `test_fail_booking_releases_its_held_slot`). The visit paths write **no** slot status: completing a visit and marking a no-show leave the slot `booked` with `version` unchanged — asserted by the T3 service tests `test_complete_finishes_visit_and_appointment_and_leaves_the_slot_booked` and `test_no_show_with_no_visit_marks_it_and_leaves_the_slot_booked`. The end-to-end tests `test_a_visit_runs_from_booking_to_completion` and `test_nobody_checking_in_becomes_a_no_show_on_the_real_timer` assert the slot's status only. `tests/meta/test_slot_writes.py` fails if any file in `app/` other than `service.py` writes slot status |
| **R-3** | A slot in the past is still bookable. `CHECK (starts_at > now())` is impossible — PostgreSQL requires CHECK conditions to be immutable | **Closed.** `service.ensure_slot_bookable` rejects a slot whose `starts_at <= now` with `Unprocessable` — a permanent, non-retried failure that compensates — in the `validate_booking` activity, and `service.claim_slot` re-checks at time of use with a `starts_at > now` predicate in the claim's `WHERE` — a retry or a backlogged worker can reach the claim after the slot started. Proven by T3 `test_a_past_slot_is_rejected`, `test_a_slot_that_started_before_the_claim_is_unprocessable` and, end to end, `test_a_past_slot_fails_and_leaves_the_slot_free` |
| **R-4** | `provider_slots.version` does not self-increment. The claim statement must say `version = version + 1` | **Closed.** Every slot write in `app/modules/scheduling/service.py` — `claim_slot`, `confirm_booking`, `fail_booking` — sets `version = ProviderSlot.version + 1`. Proven by T3 `test_claim_increments_version_and_is_idempotent` (1 after a claim and its retry), `test_confirm_books_the_slot_and_is_idempotent` and `test_fail_booking_releases_its_held_slot` (2 each), and the round trip `test_a_booking_is_confirmed_end_to_end` (2) |
| **R-5** | A patient cannot read their own record. This needs per-object authorisation, not per-role | **Read half closed** (2026-10-28): `GET /patients/me` resolves the caller's login to its linked record (`patients.user_id`), 404 if none. T1 `test_me_requires_a_token`, `test_me_is_for_patients_only` (403, not 422, so `/me` is routed before `/{patient_id}`); T3 `test_a_patient_reads_their_own_record`, `test_a_login_with_no_linked_record_is_a_404`; case `pat-003`. **Open:** listing a patient's own appointments, slice 5 |
| **R-6** | Authentication failures (401/403) did not carry the `error` key used by every other error response | **Closed.** `require_role` now raises `DomainError` subclasses instead of `HTTPException`, so 401/403 route through the same handler as everything else. `DomainError` gained a `headers` attribute so the RFC 9110 `WWW-Authenticate` challenge survives the change. Guarded by `sys-007` |
| **R-7** | Redis is connected and health-checked but used by no feature | **Open.** Intended use is caching and rate limiting in Week 3 |
| **R-8** | The monolith's blast radius: one bad deploy takes down every module, and the API scales as a unit | **Accepted** for the five-week horizon, and stated rather than mitigated. The Temporal worker scales independently from Week 2, the Celery worker from Week 3, which covers the loads most likely to spike first. The trigger for revisiting is release contention between teams or a measured load profile — neither of which exists yet, since **nothing has been load-measured** |
| **R-9** | A booking workflow **terminated** (not cancelled) from the Temporal UI or CLI runs no workflow code, so `fail_booking` never runs: a `held` slot and a `pending` appointment are stranded | **Accepted; operational rule "cancel, never terminate".** Cancellation raises inside the workflow and compensates (Week 2 spec §6, verified on temporalio 1.34). A reconciler for stale `pending` rows is **deferred** — needed only if someone terminates a workflow (D-10) |
| **R-10** | A failed appointment keeps its `slot_id`. The partial index ignores failed rows, but the `appointments.slot_id` foreign key's `ON DELETE RESTRICT` blocks deleting a slot that has failed bookings | **Accepted.** The one code path that deletes slots, `seed --clear` (`app/seed.py`), therefore fails on any seeded slot an appointment references, failed ones included. Slot regeneration (deferred, ARCHITECTURE §3.6) must account for it (`fail_booking` docstring, `app/modules/scheduling/service.py`) |
| **R-11** | `GET /appointments/{id}` for an id with **no row yet** answers from Temporal — `pending` while the workflow runs, `failed` once it has closed — with no ownership check, because there is no row to read the patient from | **Accepted.** The synthetic answer exposes only `id`, `status` and `in_progress`. Once the row exists, a patient reading another patient's appointment gets 404 (T3 `test_a_patient_cannot_read_another_patients_appointment`) |
| **R-12** | A duplicate audit entry. The audit write precedes the commit, so a booking activity whose commit fails and which Temporal then retries records the change twice. Extends R-1 to the activities | **Accepted**, as R-1. Each booking operation audits only when it actually wrote, so a retry after a *committed* first run adds no entry (`app/modules/scheduling/service.py` module docstring) |
| **R-13** | `POST /appointments` can answer 503 after the workflow **did** start: the start succeeded, then the fingerprint check's `describe()` could not reach Temporal | **Accepted.** Retrying with the same `Idempotency-Key` is safe and returns the same booking (D-11). A client that sent no key cannot tell, and a retry books afresh — which is what the key is for |
| **R-14** | Provider per-object authorisation: `POST /appointments/{id}/seen` and `/complete` are gated by role alone, so **any** provider (and admin) may mark **any** appointment's visit seen or complete, not only their own patients'. Checking ownership needs a provider-identity lookup across the module boundary (D-8, D-15) | **Accepted for Week 2**; role-gating only. The audit trail records the acting user, so a wrong action is attributable. Closing it needs a published `providers` interface, like `patients.public` |
| **R-15** | A booking confirmed **before** slice 3 deployed has no `visit-<id>` workflow, so it is never automatically marked no-show. Its check-in still works: the endpoint writes the row and the signal's `NOT_FOUND` is ignored (T3 `test_a_booking_confirmed_before_the_visit_workflow_existed_can_still_check_in`). Booking's `workflow.patched("booking-starts-visit")` keeps such in-flight bookings replaying (T2 `test_a_pre_visit_booking_history_still_replays`) but does not start a timer for them | **Accepted**; a one-off backfill that starts a timer for each such confirmed appointment is deferred |
| **R-16** | A check-in in the instant between `confirm` committing and the visit workflow starting is recorded, but its `checked_in` signal finds no workflow (`NOT_FOUND`, ignored) and is dropped. The timer then runs to the deadline and ends `visit_exists` | **Accepted** — correct, only later: the visit row is the record, so the appointment is never marked no-show (`mark_no_show` finds the visit under the appointment lock), and the workflow merely closes at the deadline instead of at the check-in. Proven for the lost-signal case by T2 `test_with_no_signal_the_timer_fires_and_the_database_decides` and T3 `test_no_show_after_a_committed_check_in_is_visit_exists` |

## 10. Delivery milestones

| Week | Focus | Deliverables | Status |
| --- | --- | --- | --- |
| **1** | Foundation & core services | Working patient/provider APIs, auth, ten-table schema with migrations, Docker setup, clean module structure | **Complete** |
| **2** | Scheduling & workflows | Temporal booking workflow, slot reservation, visit lifecycle, duplicate prevention, appointment state machine | In progress |
| **3** | Events & observability | Kafka integration, Celery workers, notification flows, analytics metrics, tracing and dashboards | Planned |
| **4** | Ingestion & retrieval | PDF parsing, chunking, embeddings, vector DB, semantic retrieval | Planned |
| **5** | AI layer | Patient assistant, booking guidance, communication generation, streaming, AI metrics | Planned |

## 11. Traceability

Three links connect a requirement to shipped, tested code.

**① Requirement → module**

| Requirement | Module / artefact |
| --- | --- |
| `PART-A-FR-1` | `app/modules/identity`, `app/modules/patients`, `app/modules/providers`, `app/core/audit.py`, `app/cli.py` |
| `PART-A-FR-2` | `app/modules/scheduling/` — `router.py`, `booking.py`, `workflows.py`, `activities.py`, `service.py`, `state.py`, `contracts.py`, `schemas.py`, `models.py`; `app/modules/patients/public.py`; registered in `app/components.py`, run by `app/worker.py` |
| `PART-A-FR-3` | `app/modules/scheduling/` — `router.py` (the four visit endpoints), `service.py` (`check_in`, `mark_seen`, `complete_visit`, `mark_no_show`, `no_show_deadline`, `get_visit`), `state.py` (`VisitAction`, `require_visit_action`, `NoShowOutcome`), `visits.py` (the best-effort `checked_in` signal), `workflows.py` (`VisitWorkflow`), `activities.py` (`visit.deadline`, `visit.no_show`), `models.py` (`Visit`); `Settings.no_show_grace_minutes` |
| `PART-A-FR-4` | — not started |
| `PART-A-FR-5` | — not started; schema support in `app/modules/scheduling/models.py` |
| `PART-A-OBS-1` | `app/api/health.py`, `app/core/logging.py`, `app/api/error_handlers.py`, `app/worker.py` (`--check`) |
| `PART-A-NFR-1` | `migrations/versions/0001_baseline.py` (`ux_appointments_live_slot`, GiST exclusion) |
| `PART-A-NFR-6` | `migrations/versions/0002_coherence_constraints.py`, `app/db/mongo.py` |

**② Requirement → test case.** Every case in `tests/cases/*.yaml` declares a `requirement`
field carrying an id from this document. A case declaring none is a traceability gap.

**③ Generated coverage report.** `tests/reports/traceability.md` (git-ignored) is
regenerated by `python -m tests.runner.route_check --write-traceability` and lists each
requirement id with the cases covering it; `--write-catalog` regenerates the tracked
`tests/cases/CATALOG.md`. Current state:

| requirement | cases |
| --- | --- |
| `PART-A-FR-1` | 8 |
| `PART-A-FR-2` | 6 — `bkg-001`, `bkg-004`, `bkg-005`, `bkg-006`, `bkg-007`, `slt-001` |
| `PART-A-FR-3` | 7 — `vst-001`, `vst-002`, `vst-004`, `vst-005`, `vst-006`, `vst-007`, `vst-008` |
| `PART-A-NFR-1` | 2 — `bkg-003`, `vst-005` |
| `PART-A-NFR-2` | 4 — `bkg-002`, `sys-008` (every workflow registered and sandbox-valid), `vst-003` (a pre-visit booking history still replays) and `vst-007` (the visit timer outlasts a database outage) |
| `PART-A-NFR-5` | 2 |
| `PART-A-OBS-1` | 6 |
| `PART-A-OBS-4` | 1 |

34 cases, as of 2026-10-29 (a case naming two requirements is counted under each, so the rows sum to 36). All other ids are uncovered because the features they describe are not
yet built. Recount with `grep -h '^requirement:' tests/cases/*.yaml | sort | uniq -c`.

---

## Appendix — requirement id scheme

```
PART-{A|B}-{FR|OBS|NFR}-{n}[.{m}]
             │    │    └── sub-requirement, where one item needs finer tracking
             │    └─────── FR  functional
             │             OBS observability & reliability
             │             NFR non-functional / quality attribute
             └──────────── the part of the assignment
```

Ids are **stable**. Once a test case references one, it is not renumbered — a superseded
requirement is marked obsolete in place rather than removed.
