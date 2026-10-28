# Week 2 — Scheduling and Service Workflows — Design

**Date:** 2026-10-28
**Status:** Approved (design); implementation plan pending
**Depends on:** `2026-09-02-week1-foundation-design.md` (schema, constraints),
`2026-09-03-week1-management-apis-design.md` (request path, auth, audit),
`docs/ARCHITECTURE.md` §3 (the booking and cancellation flows designed in Week 1)

---

## 1. Scope

Week 2 of `docs/requirements/execution-guidelines.md`: *"appointment scheduling workflow
using Temporal; slot reservation logic; visit lifecycle handling; duplicate prevention;
state transitions and validations"*, delivering *"stable scheduling workflow; provider
orchestration ready; reliable appointment state machine"*.

**In scope**

- Temporal plumbing: dependency, client, worker process, compose service, registries.
- Booking: `POST /appointments` → `BookAppointmentWorkflow`; `GET /appointments/{id}`.
- Visit lifecycle: `VisitWorkflow`, signalled by check-in / seen / complete, with a durable
  no-show timer.
- Cancel and reschedule as workflows, including the paired-write slot release.
- Waitlist: `POST /waitlist`, and promotion of a waitlisted patient onto a freed slot.
- Patient self-service (PRD R-5): a patient reads their own record and their own bookings.
- Closing PRD R-2, R-3, R-4, R-5.

**Out of scope — and where it goes**

| Not built | Reason | Lands |
| --- | --- | --- |
| Kafka publishing of `appointments.*` / `visits.completed` | Kafka is Week 3's scope | Week 3 |
| Real notification delivery | Celery/RabbitMQ is Week 3's scope | Week 3 |
| Real billing (`billing_records`, refunds) | Its shape follows the real integration; inventing columns now guarantees a rewrite | Week 3 or later |
| Provider availability generator | Seeded slots suffice to exercise every flow; the cadence question (ARCHITECTURE §3.6 item 2) is unchanged | Later, explicitly deferred |
| Per-object provider authorisation (a provider may only signal *their own* visits) | Needs a provider-identity lookup across the module boundary; role-gating is sufficient for Week 2 | Recorded as a known gap |

Billing pre-check, refund, and reminder scheduling are **stand-in activities** in Week 2:
the billing pre-check always approves, reminders and refunds emit a structured log line.
They exist so the workflow has its final shape now; Week 3 replaces their bodies, not the
workflows.

## 2. Decisions

Each is numbered so the PRD can adopt it (§8 continues from D-8).

| Id | Decision | Rejected alternative | Why |
| --- | --- | --- | --- |
| **D-9** | `ProviderSlot` moves from `providers` into `scheduling` | (a) `providers` publishes claim/release functions, one allowed edge; (b) scheduling writes raw SQL on `provider_slots` | Slots are bookable inventory and scheduling is the only module that mutates them. The paired write (appointment + slot in one transaction) stays inside one module, and the horizontal import rule needs no exception for it. (a) makes the paired write straddle a boundary; (b) passes the import test while coupling to another module's table behind its back. The table, constraints and migrations are untouched — only the Python class moves |
| **D-10** | The booking workflow creates the `pending` row; the router only starts the workflow | Router inserts the row, then starts the workflow | Two systems cannot be written atomically. Workflow-first means a failed start leaves nothing behind, and a started workflow is guaranteed by Temporal to reach `confirmed` or `failed`. Router-first leaves a permanently `pending` orphan whenever the start fails, which needs a reconciler to clean up |
| **D-11** | Duplicate submission is prevented by deterministic workflow ids: `Idempotency-Key` → `appointment_id` → `booking-<appointment_id>` | Dedupe table keyed on request hash | Temporal already refuses a second start under a running workflow id; a second store would be a second source of truth for the same fact |
| **D-12** | The visit lifecycle is one long-running `VisitWorkflow` per confirmed appointment, driven by signals, with a durable no-show timer | Transactional endpoints with a state machine, no-show by periodic job | No-show detection needs no scheduler — Temporal is one. Every visit's history is inspectable, which is the "real-time status visibility" and "recovery from failures" `PART-A-FR-3` asks for |
| **D-13** | Waitlist promotion is an activity of the cancel/reschedule workflows in Week 2 | Defer promotion to a Week 3 Kafka consumer | "Waitlist movement" is a named cancellation effect. In Week 3 the activity is replaced by publishing `appointments.cancelled` and the promotion moves to a consumer |
| **D-14** | Reschedule books the new slot **before** releasing the old one | Cancel then book | A failed reschedule must leave the patient with their original appointment, not with nothing |
| **D-15** | `patients` exposes a published interface, `app/modules/patients/public.py`; scheduling may import that file and nothing else from `patients` | Scheduling queries the `patients` table directly | A read-only query against another module's table is still coupling to its schema. One declared edge onto one file is visible in a diff and in the meta-test |

**R-2 restated.** The Week 1 wording — "cancel, reschedule, no-show and completion must free
the slot" — is wrong for two of the four: a completed or no-show appointment's slot is in the
past and must not return to `free`. The real invariant is **slot status agrees with
appointment status, written in one transaction**:

| Appointment moves to | Slot becomes |
| --- | --- |
| `confirmed` | `booked` |
| `failed` (compensation) | `free`, if this appointment held it |
| `cancelled` | `free` |
| `rescheduled` | `free` |
| `completed` | stays `booked` |
| `no_show` | stays `booked` |

## 3. Module layout

```
app/modules/scheduling/
    models.py       Appointment, Visit, WaitlistEntry, ProviderSlot (moved, D-9)
    state.py        appointment and visit transition tables — pure, no I/O
    service.py      async DB operations; raise domain errors; callable from routers and activities
    activities.py   Temporal activities: own session, call one service function, commit
    workflows.py    BookAppointment, CancelAppointment, RescheduleAppointment, Visit
    schemas.py      request/response models
    router.py       HTTP surface (framework-bound)
app/modules/patients/public.py   get_bookable_patient(), patient_id_for_user() (D-15)
app/core/temporal.py             client construction from Settings
app/core/registry.py             + WORKFLOWS and ACTIVITIES registries
app/worker.py                    python -m app.worker — runs every registered workflow/activity
```

**Process topology.** The API and the worker are one codebase, two processes. The worker
listens on `Settings.task_queue("scheduling")` (harness requirement 1) and is a `worker`
service under the compose `app` profile. They scale independently, which is the mitigation
PRD R-8 claims.

**Import rules.**

- `workflows.py` and `activities.py` are framework-free by default — the vertical rule's
  allowlist already excludes them, so a FastAPI import in either fails the suite.
- `workflows.py` imports no database, settings or clock code: everything non-deterministic
  happens in activities. Time inside a workflow is `workflow.now()`.
- The horizontal rule gains exactly one declared edge: `scheduling.* → patients.public`.
- `providers` loses `ProviderSlot`/`SlotStatus`; `app/seed.py` and `app/db/all_models.py`
  import them from `scheduling`.

**The single release path.** Slot status is written by exactly two service functions:
`claim_slot` (`free → held`) and `transition_appointment` (which applies the R-2 table in
the same transaction as the appointment write). A meta-test fails if any other code writes
`ProviderSlot.status` or `provider_slots.status`.

## 4. Flows

### 4.1 Booking

`POST /appointments` — body `{patient_id, slot_id, reason?}`, optional `Idempotency-Key`
header. Roles: `patient` (own `patient_id` only, via `patient_id_for_user`), `front_desk`,
`admin`.

Router:

1. Authorise. A patient booking for anyone else is 403.
2. `appointment_id = uuid5(NAMESPACE, f"{caller_user_id}:{key}")` if the header is present,
   else `uuid4()`. The caller is part of the hash so two users cannot collide on a key.
3. Start `booking-<appointment_id>`. If that id is already running or completed, return the
   same 202 — the replay is answered, not rejected.
4. Return **202** `{appointment_id, status: "pending"}`. Temporal unreachable → 503.

`BookAppointmentWorkflow(appointment_id, patient_id, slot_id, reason, rescheduled_from_id?, waitlist_entry_id?)`:

| Step | Activity | Failure |
| --- | --- | --- |
| 1 | `create_pending` — `INSERT … ON CONFLICT (id) DO NOTHING`; provider, clinic, department copied from the slot | slot not found → permanent |
| 2 | `validate_booking` — patient bookable (`patients.public`); slot `starts_at > now()` (**R-3**) | permanent |
| 3 | `claim_slot` — `UPDATE provider_slots SET status='held', version=version+1 WHERE id=:id AND status='free'` (**R-4**), and attach `slot_id` to the appointment, one transaction | 0 rows → `Conflict`, permanent, **not retried** |
| 4 | `billing_precheck` (stand-in) | permanent if declined |
| 5 | `schedule_reminders` (stand-in) | retryable |
| 6 | `confirm` — appointment `confirmed`, `confirmed_at`; slot `booked` | retryable |
| 7 | start `visit-<appointment_id>` as an independent workflow, not a child, so it outlives booking; if `waitlist_entry_id` was given, mark that entry `fulfilled` | retryable |

Any failure after step 1 runs `compensate_booking`: set the appointment `failed` and, only
if this appointment holds the slot, return it to `free`. Idempotent; re-running it is a
no-op. The workflow then ends failed with the original cause.

`GET /appointments/{id}` returns the row. If no row exists but `booking-<id>` is known to
Temporal, it returns `{appointment_id, status: "pending", in_progress: true}` — the gap
between 202 and step 1 is milliseconds, but a client polling immediately must not see 404.
Roles: the owning patient, `provider`, `front_desk`, `admin`.

### 4.2 Visit

`VisitWorkflow(appointment_id, slot_ends_at)`, started by booking step 7.

- Waits for `check_in`; then `seen`; then `complete`. Each signal runs one activity that
  checks `state.py` against the database row and writes the visit: `check_in` inserts the
  `visits` row (`checked_in_at`); `seen` sets `seen_at` and `in_progress`; `complete` sets
  `completed_at`, the visit `completed`, and the appointment `completed` (R-2 table: slot
  stays `booked`). Re-delivering a signal for a transition already applied is a no-op.
- **No-show timer:** if `check_in` has not arrived by `slot_ends_at + Settings.no_show_grace`
  (default 15 minutes), the appointment moves to `no_show` and the workflow ends.
- **`cancel` signal:** ends the workflow without writing — the cancel/reschedule workflow
  owns that write.

Endpoints, each validating the transition against the database **before** signalling so an
illegal one is a synchronous 409 rather than a silently ignored signal:

| Endpoint | Roles |
| --- | --- |
| `POST /appointments/{id}/check-in` | `front_desk`, `admin` |
| `POST /appointments/{id}/seen` | `provider`, `admin` |
| `POST /appointments/{id}/complete` | `provider`, `admin` |
| `GET /appointments/{id}/visit` | owning patient, `provider`, `front_desk`, `admin` |

### 4.3 Cancel

`POST /appointments/{id}/cancel` — `{reason}`. Roles: owning patient, `front_desk`, `admin`.
Legal only from `pending` or `confirmed`, checked synchronously → 409 otherwise. Starts
`cancel-<appointment_id>` (a second cancel is answered with the same 202) and returns 202.

`CancelAppointmentWorkflow`:

1. `transition_appointment(→ cancelled)` — appointment `cancelled`, `cancelled_at`,
   `cancellation_reason`; slot `free`; one transaction.
2. Signal `cancel` to `visit-<id>`; "workflow not found" is success.
3. `refund` (stand-in).
4. `promote_waitlist(slot_id)` — §4.5.

A cancel racing an in-flight booking (appointment still `pending`): the cancel workflow's
transition only applies from `pending`/`confirmed` and takes a row lock, so whichever commits
first wins; the booking's subsequent `confirm` finds the appointment no longer `pending`,
fails permanently, and its compensation is a no-op because the slot is already `free`.

### 4.4 Reschedule

`POST /appointments/{id}/reschedule` — `{new_slot_id}`. Same roles and legality as cancel.
Starts `reschedule-<id>`, returns 202 with the new appointment id.

`RescheduleAppointmentWorkflow`:

1. Run `BookAppointmentWorkflow` as a **child** for a new appointment with
   `rescheduled_from_id = id`.
2. Child failed → stop. The new appointment is `failed`, the original is untouched (D-14).
3. Child confirmed → `transition_appointment(old → rescheduled)` (slot `free`); signal
   `cancel` to the old `visit-<id>`; `promote_waitlist(old_slot_id)`.

### 4.5 Waitlist

`POST /waitlist` — `{patient_id, provider_id?, department_id?, window_start, window_end}`;
same patient-ownership rule as booking. `GET /waitlist?patient_id=` for staff.

`promote_waitlist(slot_id)`: select the oldest `active` entry whose window contains the
slot's `starts_at` and whose provider/department, when set, match the slot — `FOR UPDATE
SKIP LOCKED`, so two concurrent promotions never take one entry. Start
`booking-wl-<entry_id>` for that patient on that slot. When that booking confirms, its final
activity marks the entry `fulfilled`; when it fails, the entry stays `active`. The workflow
id makes a retried promotion start nothing new.

### 4.6 Patient self-service (R-5)

`GET /patients/me` and `GET /appointments?mine=true` for the `patient` role, resolved via
`users.id → patients.user_id`. A patient with no linked record gets 404.

## 5. State machines

`state.py` holds both machines as data. Every write path asks it before writing; the T0
tests enumerate every pair.

```
appointment:  pending   → confirmed | failed | cancelled
              confirmed → cancelled | rescheduled | completed | no_show
              (cancelled, rescheduled, completed, no_show, failed are terminal)

visit:        (none) → checked_in → in_progress → completed
```

An appointment may move to `completed` only from `confirmed` with a visit in `in_progress`;
`no_show` only from `confirmed` with no visit row.

## 6. Error handling

| Situation | Handling |
| --- | --- |
| Database/connection error in an activity | Propagates; Temporal retries with backoff (max 5 attempts, configurable) |
| `Conflict`, `NotFound`, `ValidationError` in an activity | Converted to `ApplicationError(non_retryable=True)`; the workflow compensates at once |
| Worker crash mid-workflow | Temporal replays the workflow on another worker; activities are idempotent so a re-run of a completed step changes nothing |
| Worker crash mid-compensation | Same — compensation is idempotent |
| Temporal unreachable at the API | 503 in the standard error envelope |
| Signal for an illegal transition | Rejected synchronously at the endpoint (409); the activity re-checks and no-ops if it slips through |

Every appointment and visit transition is audited to Mongo with the same envelope Week 1
uses, actor being the requesting user for HTTP-initiated steps and `system:<workflow>` for
timer-initiated ones (no-show).

## 7. Testing

The cheapest tier that proves each property:

| Tier | Proves |
| --- | --- |
| T0 unit | Every allowed and forbidden transition in `state.py`; idempotency-key derivation |
| T1 contract | Routes, role gates, 202/403/404/409/503, fake Temporal client |
| T2 workflow (time-skipping, mocked activities) | Confirm only after all steps; each failing step → `failed` and compensation called; conflict not retried; reschedule failure leaves original confirmed; visit signal order; no-show timer fires; `cancel` signal ends the visit workflow |
| T3 integration (compose Postgres) | Two concurrent `claim_slot` → exactly one wins and `version` incremented once; every R-2 row leaves slot and appointment agreeing; `create_pending` idempotent; past slot rejected; `promote_waitlist` under concurrency takes one entry |
| T4 journey (compose Temporal + worker) | Book → confirmed → check-in → seen → complete; book → cancel → waitlisted patient confirmed |
| Meta | Workflow determinism (sandbox-validate every registered workflow); single slot-status write path; the one declared edge onto `patients.public` |

Each flow gets case YAML under `tests/cases/`, authored with the `smarthealth-testcase`
skill.

## 8. Delivery

One branch per slice. Each lands when its tests pass, by fast-forwarding `main` and pushing.

| # | Branch | Contents |
| --- | --- | --- |
| 1 | `feat/temporal-worker` | `temporalio` dependency, `app/core/temporal.py`, registries, `app/worker.py`, compose `worker` service, determinism meta-test, T2 fixture |
| 2 | `feat/booking-workflow` | D-9 slot move, `patients/public.py`, `state.py`, booking workflow and endpoints |
| 3 | `feat/visit-workflow` | `VisitWorkflow`, visit endpoints, no-show timer |
| 4 | `feat/cancel-reschedule` | Cancel, reschedule, waitlist endpoints and promotion |
| 5 | `feat/patient-self-service` | R-5 |

Every slice updates, in the same branch: `docs/PRD.md` (milestone status, traceability,
D-9…D-15, R-2…R-5 dispositions), `docs/ARCHITECTURE.md` §3 (moved from "design" to "built"
section by section — never ahead of the code), the README endpoint table, and `CLAUDE.md`'s
status and harness-requirement tally (requirements 1 and 3 become satisfied in slice 1).
