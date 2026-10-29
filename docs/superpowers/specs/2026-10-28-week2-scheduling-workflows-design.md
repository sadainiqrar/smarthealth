# Week 2 — Scheduling and Service Workflows — Design

**Date:** 2026-10-28
**Status:** Approved (design). Slices 1–5 built.
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
- Visit lifecycle: check-in / seen / complete written synchronously by the endpoints, and a
  `VisitWorkflow` per confirmed appointment that owns the durable no-show timer (D-12, as
  amended 2026-10-29).
- Cancel as a synchronous paired write with a follow-up workflow; reschedule as a workflow.
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
| Provider deactivation handling — what happens to a deactivated provider's existing future appointments | *Added 2026-10-29; omitted from this table originally,* although the Week 1 spec promised it for Week 2. Its trigger crosses the module boundary the wrong way (`providers` may not import `scheduling`), which is what Week 3's domain events are for, and its policy (cancel and notify, reassign, or refuse) is undecided. New bookings for an inactive provider are refused (PRD R-24) | Week 3 (PRD R-25) |
| Clinic and department endpoints (PRD `PART-A-FR-1.5`) | *Added 2026-10-29.* Seeded data covers every flow; the endpoints are additive CRUD | Deferred |

Billing pre-check, refund, and reminder scheduling are **stand-in activities** in Week 2:
the billing pre-check always approves, reminders and refunds emit a structured log line.
They exist so the workflow has its final shape now; Week 3 replaces their bodies, not the
workflows.

## 2. Decisions

Each is numbered so the PRD can adopt it (§8 continues from D-8).

| Id | Decision | Rejected alternative | Why |
| --- | --- | --- | --- |
| **D-9** | `ProviderSlot` moves from `providers` into `scheduling` | (a) `providers` publishes claim/release functions, one allowed edge; (b) scheduling writes raw SQL on `provider_slots` | Slots are bookable inventory and scheduling is the only module that mutates them. The paired write (appointment + slot in one transaction) stays inside one module, and the horizontal import rule needs no exception for it. (a) makes the paired write straddle a boundary; (b) passes the import test while coupling to another module's table behind its back. The table, constraints and migrations are untouched — only the Python class moves |
| **D-10** | The booking workflow creates the `pending` row; the router only starts the workflow | Router inserts the row, then starts the workflow | Two systems cannot be written atomically. Workflow-first means a failed start leaves nothing behind, and a started workflow is guaranteed by Temporal to reach `confirmed` or `failed`. Router-first leaves a permanently `pending` orphan whenever the start fails, which needs a reconciler to clean up. Workflow-first needs none — *unless someone terminates a workflow*: termination skips compensation, so the operational rule is "cancel, never terminate", and a reconciler for stale `pending` rows is deferred (§6) |
| **D-11** | Duplicate submission is prevented by deterministic workflow ids: `Idempotency-Key` → `appointment_id` → `booking-<appointment_id>` | Dedupe table keyed on request hash | Temporal already refuses a second start under a running workflow id; a second store would be a second source of truth for the same fact |
| **D-12** | *Amended 2026-10-29.* **The database records human actions; the workflow owns time.** Check-in, seen and complete are written synchronously by their endpoints, in the request's transaction, under the appointment row lock, and answer 200. One `VisitWorkflow` per confirmed appointment is only the durable no-show timer; at the deadline it asks the database, under the same lock, whether anyone checked in | (a) *The original D-12:* a long-running workflow driven by staff signals, writing each action through an activity after a 202 — a review proved it loses check-ins (§4.2); (b) Temporal Updates, so the endpoint waits for the workflow's write — considered and not chosen: the write still lives behind the workflow, every action then depends on Temporal being up, and the no-show race still has to be settled in the database; (c) no-show by periodic job — needs a scheduler, and Temporal is one | Each human action is recorded the moment staff are told it was, so a timer can never overrule it, and a Temporal outage cannot fail one. The timer keeps what Temporal is uniquely good at: a durable per-appointment deadline that survives restarts, with an inspectable history |
| **D-13** | Waitlist promotion is part of the cancel and reschedule workflows in Week 2: the shared workflow code `_promote_waitlist` (`app/modules/scheduling/workflows.py`) over the `waitlist.candidates` and `waitlist.fulfil` activities, each candidate tried as a child booking `booking-wl-<entry_id>` with `ALLOW_DUPLICATE_FAILED_ONLY` | Defer promotion to a Week 3 Kafka consumer | "Waitlist movement" is a named cancellation effect. In Week 3 the promotion moves to a consumer of `appointments.cancelled`, and the child-booking id stays its guard |
| **D-14** | Reschedule books the new slot **before** releasing the old one | Cancel then book | A failed reschedule must leave the patient with their original appointment, not with nothing |
| **D-15** | `patients` exposes a published interface, `app/modules/patients/public.py`; scheduling may import that file and nothing else from `patients` | Scheduling queries the `patients` table directly | A read-only query against another module's table is still coupling to its schema. Two declared edges (from `scheduling.activities` and `scheduling.router`) onto one file are visible in a diff and in the meta-test |
| **D-16** | **Cancel is a synchronous paired write answering 200**; its follow-ups (visit timer, refund, waitlist promotion) run in `CancelFollowUpWorkflow`, started best-effort after commit. Reschedule stays a workflow (202) because it must book first | The original §4.3: a `CancelAppointmentWorkflow` doing the transition itself after a 202 | Amended D-12's reasoning: a 202 followed by an asynchronous write races with the visit timer and a concurrent booking, and a Temporal outage would then fail a cancel the patient was promised. The cost is that a failed follow-up start loses the follow-ups -- a known gap closed by Week 3's transactional outbox |

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
    contracts.py    BookingRequest and the other dataclasses passed to workflows and activities
    booking.py      starting a booking: id derivation and the workflow-start policy
    followups.py    starting the cancel follow-up (best-effort) and the reschedule (503 on failure)
    workflows.py    BookAppointment, Visit, CancelFollowUp, Reschedule
    visits.py       notify_checked_in — the best-effort check-in signal sent after the request's commit
    schemas.py      request/response models
    router.py       HTTP surface (framework-bound)
app/modules/patients/public.py   ensure_bookable_patient(), patient_id_for_user() (D-15)
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
- The horizontal rule gains declared edges onto `patients.public` from
  `scheduling.activities` and `scheduling.router` only. Not from `scheduling.service`:
  rule 4 of `tests/meta/test_import_boundaries.py` (`NEVER_CROSS`) forbids `service.py`,
  `models.py` and `schemas.py` any cross-module import, allowlist or not. The activity
  performs the patient check and the service receives only ids. (Corrected 2026-10-28 while
  planning slice 2.)
- `providers` loses `ProviderSlot`/`SlotStatus`; `app/seed.py` and `app/db/all_models.py`
  import them from `scheduling`.

**The single release path.** Slot status is written only in `scheduling/service.py`, each
write in the same transaction as the appointment write that justifies it (the R-2 table):
`claim_slot` (`free → held`), `confirm_booking` (`held → booked`) and `fail_booking`
(`held → free`), joined by the cancel and reschedule paths (the visit paths write no slot) as later slices add them there. The
meta-test (`tests/meta/test_slot_writes.py`) enforces the file, not a list of functions: it
fails if any other code in `app/` writes `ProviderSlot.status` or `provider_slots.status`.

## 4. Flows

### 4.1 Booking

`POST /appointments` — body `{patient_id, slot_id, reason?}`, optional `Idempotency-Key`
header. Roles: `patient` (own `patient_id` only, via `patient_id_for_user`), `front_desk`,
`admin`.

Router:

1. Authorise. A patient booking for anyone else is 403.
2. `appointment_id = uuid5(NAMESPACE, f"{caller_user_id}:{key}")` if the header is present,
   else `uuid7()` (time-ordered like every other key; a keyed id is uuid5 by necessity, as it
   must be derivable). The caller is part of the hash so two users cannot collide on a key.
   An empty header is 422.
3. Start `booking-<appointment_id>`. If that id is already running or completed, return the
   same 202 — the replay is answered, not rejected. Start with id_conflict_policy=USE_EXISTING and id_reuse_policy=REJECT_DUPLICATE: Temporal's default allows a closed id to be started again, which would book a repeated request twice.
4. Return **202** `{appointment_id, status: "pending"}`. Temporal unreachable → 503.

**Idempotency-Key fingerprint.** The key names a *request*, not only an id. The workflow is
started with `memo={"fingerprint": sha256(patient_id, slot_id, reason)}`; after every start
(including a repeat that Temporal answers with the existing workflow) the router describes
the workflow and compares. A mismatch is **422** "Idempotency-Key reused with a different
request", per the IETF Idempotency-Key draft — answering 202 would tell the client a booking
was made that never was. A **failed** booking stays failed under its key: REJECT_DUPLICATE
means the same key replays the failure, so retrying needs a new key.

`BookAppointmentWorkflow(appointment_id, patient_id, slot_id, reason, rescheduled_from_id?, waitlist_entry_id?)` (as built in slice 2: `BookingRequest(appointment_id, patient_id, slot_id, actor_user_id, reason)`; slice 4 adds `rescheduled_from_id`, `waitlist_entry_id`):

| Step | Activity | Failure |
| --- | --- | --- |
| 1 | `create_pending` — `INSERT … ON CONFLICT (id) DO NOTHING`; provider, clinic, department copied from the slot | slot not found → permanent |
| 2 | `validate_booking` — patient bookable (`patients.public`); slot `starts_at > now()` (**R-3**) | permanent |
| 3 | `claim_slot` — `UPDATE provider_slots SET status='held', version=version+1 WHERE id=:id AND status='free' AND starts_at > :now` (**R-4**), and attach `slot_id` to the appointment, one transaction | 0 rows → `Conflict`, permanent, **not retried** |
| 4 | `billing_precheck` (stand-in) | permanent if declined |
| 5 | `schedule_reminders` (stand-in) | retryable |
| 6 | `confirm` — appointment `confirmed`, `confirmed_at`; slot `booked` | retried **without limit** (idempotent): it may have committed before a transient failure, and giving up would end the workflow failed while the row says confirmed. A non-retryable `Conflict` still ends it |
| 7 | start `visit-<appointment_id>` (the no-show timer, §4.2) — as built, a child with `ParentClosePolicy.ABANDON` so it outlives booking, and `REJECT_DUPLICATE` so it never reopens a closed visit; if `waitlist_entry_id` was given, mark that entry `fulfilled` (slice 4, built) | after `confirmed`, outside compensation; "already started" is ignored |

Any failure from step 1 on runs `compensate_booking` (as built, `fail_booking`; step 1 is
inside the compensated region because it may commit and still fail, and compensation no-ops
with no row): set the appointment `failed` and, only
if this appointment holds the slot, return it to `free`. Idempotent; re-running it is a
no-op. The workflow then ends failed with the original cause.

`GET /appointments/{id}` returns the row. If no row exists it asks Temporal for
`booking-<id>`'s state; these synthetic answers carry only `id`, `status` and `in_progress`:

| Workflow state | Response |
| --- | --- |
| running | `{status: "pending", in_progress: true}` — the gap between 202 and step 1 is milliseconds, but a client polling immediately must not see 404 |
| closed (any terminal status) | `{status: "failed", in_progress: false}` — it ended before recording the appointment, so the booking failed; reporting pending would leave a poller waiting forever |
| unknown (not found) | 404 |
| Temporal unreachable | 503, never 404: we cannot tell whether the booking exists |
Roles: the owning patient, `provider`, `front_desk`, `admin`.

### 4.2 Visit

*Rewritten 2026-10-29 with the D-12 amendment.* The database records human actions
synchronously; the visit workflow owns time.

**Staff actions are synchronous writes.** Each endpoint calls its service function in the
request's session; the function locks the appointment row, checks `state.py`
(`require_visit_action`) under that lock, writes, and the router commits and answers
**200** with the visit. The locked write is the check — there is no separate precheck to go
stale. The functions are **strict**: an illegal action, a repeat included (a second
check-in, `seen` twice, `complete` twice), is a 409, because nothing retries them and a
silent no-op would tell staff a duplicate "worked". Each is audited with the token's user
as actor.

- `check_in` inserts the `visits` row (`checked_in_at`, `checked_in`);
- `seen` sets `seen_at` and `in_progress`;
- `complete` sets `completed_at`, the visit `completed` and the appointment `completed` in
  one transaction (R-2 table: slot stays `booked`).

After a check-in commits, the router sends a plain `checked_in` signal to `visit-<id>`,
best-effort and bounded (5 s): `NOT_FOUND` is expected and ignored, any other Temporal
failure is logged and ignored. The request has already succeeded, and the timer reconciles
through the database if the signal is lost. `seen` and `complete` signal nothing.

**`VisitWorkflow(appointment_id)` is the no-show timer**, started by booking step 7 with
`REJECT_DUPLICATE` (a late start never opens a second run under a closed one):

1. `deadline` activity — the slot's end plus `Settings.no_show_grace_minutes` (default 15,
   `ge=0`).
2. If neither `checked_in` nor `cancel` has been signalled and the deadline is still ahead,
   wait for either, with the remaining time as the timeout. If the deadline has already
   passed, do not wait at all — `wait_condition(timeout=0)` raises `TimeoutError` even when
   its condition holds.
3. `checked_in` → return `"checked_in"`; `cancel` → return `"cancelled"`.
4. Otherwise `no_show` activity → `mark_no_show`, under the appointment lock, returns
   `no_show` (marked, or already was), `visit_exists` (a check-in reached the database; its
   signal was lost or late) or `closed` (cancelled, rescheduled or completed). That string
   is the workflow's result.

Both activities retry **without limit**: the timer has no compensation to fall back on, so
giving up after a short outage would leave the appointment with no no-show at all. Domain
errors (`NotFound`) stay non-retryable. Signals only set flags; no I/O happens in a handler.

**Why this shape.** A review of the first design (signal-driven writes, 202 responses)
found three ways it lost check-ins: (1) near the deadline the timer could fire before the
check-in activity ran, so staff were told "accepted" and the patient was recorded
`no_show`; (2) a check-in on an appointment past its deadline hit `wait_condition(timeout=0)`
and became a no-show although the signal was already buffered; (3) visit activities ran
on the booking step policy, so a database outage of a few minutes failed the visit
workflow for good. Writing the action in the request closes (1) — the check-in and
`mark_no_show` serialise on the appointment lock, and whichever commits first wins
visibly; never waiting a zero timeout closes (2); unlimited retries close (3). Temporal
Updates were considered and not chosen (D-12 (b)).

**Known gap.** A booking confirmed before slice 3 deployed has no visit workflow: its
check-in signal finds none (ignored), and it is **never automatically marked no-show**.
Booking's `patched("booking-starts-visit")` keeps such bookings replaying; a one-off
backfill that starts timers for them is deferred. `VisitWorkflow` itself needed no
`patched` for this change: it was never released.

| Endpoint | Roles | Answer |
| --- | --- | --- |
| `POST /appointments/{id}/check-in` | `front_desk`, `admin` | 200 `VisitRead`; 404; 409 |
| `POST /appointments/{id}/seen` | `provider`, `admin` | 200 `VisitRead`; 404; 409 |
| `POST /appointments/{id}/complete` | `provider`, `admin` | 200 `VisitRead`; 404; 409 |
| `GET /appointments/{id}/visit` | owning patient, `provider`, `front_desk`, `admin` | 200; 404 |

### 4.3 Cancel

*Amended 2026-10-29 (D-16).* `POST /appointments/{id}/cancel` -- `{reason}`. Roles: owning
patient, `front_desk`, `admin`. Legal only from `pending` or `confirmed`, and only while no
visit exists (a checked-in patient is not cancelled), and not while the reschedule that
created it is still completing -- otherwise 409.

Cancel is a **synchronous paired write** answering **200**, in the request's transaction and
under the appointment row lock: `appointments.status = cancelled` (with `cancelled_at` and
`cancellation_reason`) and `provider_slots.status = free` commit together (R-2). The reasoning
is amended D-12's: the caller needs a definitive answer, and a 202 followed by an
asynchronous write races with the visit timer and with a concurrent booking exactly as
visit check-ins did. Cancelling an already-cancelled appointment is a 409 (strict, like the
visit actions: only the endpoint calls it and nothing retries it).

After the commit the endpoint starts `CancelFollowUpWorkflow` (`cancel-<appointment_id>`),
best-effort, for the follow-ups that must happen eventually:

1. Signal `cancel` to `visit-<id>`; "workflow not found" is success.
2. `refund` (stand-in).
3. `promote_waitlist(slot_id)` -- section 4.5, only if the cancel released a slot.

**Known gap.** If starting the follow-up fails, the cancel stands and the follow-ups are lost
(no refund, no promotion; the visit timer still ends by itself, at its deadline, as
`closed`). The start failure is logged, not surfaced. Week 3's transactional outbox closes
it: the follow-up becomes an event written in the cancel's own transaction.

A cancel racing an in-flight booking (appointment still `pending`): the cancel takes the row
lock, so whichever commits first wins; the booking's next step finds the appointment no
longer `pending`, fails permanently, and its compensation is a no-op (`fail_booking` acts
only on a `pending` appointment). The slot the booking had claimed, if any, was already
released by the cancel.

### 4.4 Reschedule

`POST /appointments/{id}/reschedule` -- `{new_slot_id, reason?}`. Same roles as cancel.
Legal only from `confirmed`, with no visit and with any predecessor's reschedule settled,
checked synchronously -- otherwise 409. Reschedule is inherently a workflow -- it must book before it
releases -- so it answers **202** with the new appointment id. That id is derived like a
booking's (D-11) but in its own name space (`reschedule:<caller>:<key>`), so one
Idempotency-Key used for a booking and a reschedule names two appointments, never one.

`RescheduleWorkflow` (`reschedule-<new_appointment_id>`):

1. Run `BookAppointmentWorkflow` as a **child** for a new appointment with
   `rescheduled_from_id = id` -- id `booking-<new id>`, `REJECT_DUPLICATE`, and
   `ParentClosePolicy.ABANDON`: terminating the reschedule must not kill the booking between
   its claim and its compensation (the booking compensates for itself).
2. Child failed -> stop. The new appointment is `failed`, the original is untouched (D-14).
3. Child confirmed -> `complete_reschedule(old, new)`. It locks both appointments, in
   ascending id order. The **new** one is checked first: it must still be `confirmed` and
   still name the old one as `rescheduled_from_id`. If it was cancelled in the moment
   between confirming and this step, nothing is released and the reschedule ends
   `superseded` -- the old appointment stands; releasing it would leave the patient with
   nothing. Then the old one: if it is still `confirmed` with no visit, it moves to
   `rescheduled` and its slot to `free`, together. If it was cancelled or checked in
   meanwhile, nothing is released and the reschedule ends `superseded`. Old one
   **cancelled**: the new booking stands as the patient's only appointment. Old one
   **checked in**: both stay `confirmed` -- the old one closes with its visit, and staff can
   cancel the new one, because `require_reschedule_settled` counts a predecessor with a
   visit as settled.
   Otherwise: signal `cancel` to the old `visit-<id>`, then `promote_waitlist(old_slot_id)`.

**At most one live replacement (D-5).** Two reschedules of one appointment, accepted
together, both pass the synchronous check before either workflow writes. The database
refuses the second: the partial unique index `ux_appointments_live_reschedule` on
`appointments (rescheduled_from_id) WHERE status IN ('pending','confirmed')` (migration
0004) makes the second child's `create_pending` a permanent `Conflict`, so it compensates
(a no-op -- it claimed nothing) and its reschedule ends `failed`. Partial on status, so a
replacement that failed or was cancelled does not block another attempt.

**Reading the new appointment.** For a moment after the 202 neither the row nor the child
`booking-<id>` exists. `GET /appointments/{new id}` then describes `reschedule-<id>`:
running reads as `pending` (`in_progress`), closed as `failed`, and only an id that names
neither workflow is a 404.

### 4.5 Waitlist

`POST /waitlist` -- `{patient_id, provider_id?, department_id?, window_start, window_end}`;
same patient-ownership rule as booking. `GET /waitlist?patient_id=` for staff (`front_desk`,
`admin`) and for a patient, who sees only their own entries whatever `patient_id` says. Not
for providers: the list names every waiting patient, for any provider.

`promote_waitlist(slot_id)` is shared workflow code, run by the cancel follow-up and by
reschedule. An activity returns the candidates in order -- `active` entries whose window
contains the slot's `starts_at` and whose provider/department, when set, match the slot,
oldest first, a small bounded number. Each is tried in turn as a **child** booking with id
`booking-wl-<entry_id>` and `ALLOW_DUPLICATE_FAILED_ONLY`: a failed attempt may be retried by
a later release, while a confirmed (or still running) one can never start twice -- Temporal
refuses, and the loop moves on. The first child that confirms is followed by an activity
marking its entry `fulfilled`, and the loop stops.

This replaces the `FOR UPDATE SKIP LOCKED` design: a row lock cannot be held across a child
workflow, and the deterministic child id gives the same "never twice" guarantee without one.
Promotion children are started with `ParentClosePolicy.ABANDON`, for the reason given in
§4.4 step 1.

The candidates activity offers nothing unless the slot is still `free` and has not started
(`starts_at > now`, the clock injected) -- the same two conditions the claim enforces. A slot
taken again before promotion read it, or one already begun, would only make each candidate's
booking fail in turn.

Accepted limits, each deliberate:

- **Head-of-line blocking.** Candidates are read once, at most three (`limit=3`), oldest
  first. If all three fail -- say their bookings lose to a direct booking of the slot, or
  each has a promotion already running elsewhere -- the loop ends and nobody further down
  the waitlist is offered this release. The next release of a matching slot tries again.
  A larger or paged read would trade a bounded workflow history for a longer tail; three
  is enough while the waitlist is short.
- **Promotion appointment ids are not time-ordered.** The id of a promoted appointment is
  `workflow.uuid4()`: deterministic, so a replay derives the same id, but a random v4
  rather than the uuid7 every other primary key uses -- a deliberate exception to
  PART-A-NFR-5. Nothing time-based is replay-safe in a workflow except `workflow.now()`,
  and hand-building a v7 from it is code spent on index locality for a rare write.
- **Cancelling after the slot started is allowed.** Front desk may cancel an unattended
  appointment before the no-show timer fires; the cancel frees the slot (R-2) as any cancel
  does. With the `starts_at > now` check above, such a slot is not offered to the waitlist:
  nobody can be booked into a slot that has begun (R-3).

### 4.6 Patient self-service (R-5)

`GET /patients/me` and `GET /appointments`, resolved for the `patient` role via
`users.id → patients.user_id`. There is no `mine` flag: a patient's list is always their
own, forced server-side, so a flag would only be a way to ask for something else. A
patient naming another `patient_id` gets 403; a patient login with no linked record gets
404 (an empty list would hide a misissued account).

Staff (provider, front desk, admin) get the same endpoint with filters: `patient_id`,
`provider_id`, `status`, and a slot window `from` (inclusive) / `to` (exclusive) on
`provider_slots.starts_at`, timestamps with an offset (422 otherwise). The result is
paginated, newest slot first; an appointment with no slot yet sorts last and is excluded
whenever a window bound is given. Each item carries `starts_at`, the slot's start, so a
client can show when without a second call. Provider per-object scoping stays the
documented gap R-14.

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
| Database/connection error in an activity | Propagates; Temporal retries with backoff. Booking steps 1–5 are bounded (5 attempts, then the workflow compensates); `confirm`, the compensation and both visit activities retry without limit (`workflows.py`: `CONFIRM`, `COMPENSATION`, `VISIT_STEP`) |
| Any `DomainError` (`Conflict`, `NotFound`, `Unprocessable`, …) in an activity | Converted to `ApplicationError(non_retryable=True)`; the workflow compensates at once |
| Worker crash mid-workflow | Temporal replays the workflow on another worker; activities are idempotent so a re-run of a completed step changes nothing |
| Worker crash mid-compensation | Same — compensation is idempotent |
| Temporal unreachable at the API | 503 in the standard error envelope — including `GET` for an id with no row, which returns 503, not 404 |
| Malformed id reaching an activity | `ApplicationError(type="InvalidRequest", non_retryable=True)` before the unit of work opens; never retried |
| Workflow **terminated** (not cancelled) from the Temporal UI/CLI | Termination runs no workflow code, so compensation is skipped: a held slot and a `pending` row are stranded. Operational rule: **cancel, never terminate** — cancellation raises inside the workflow and compensates (verified on temporalio 1.34: a booking cancelled mid-`claim_slot` runs `fail_booking` and ends failed). A reconciler for stale `pending` rows is **deferred**; it is needed only if someone terminates a workflow (D-10) |
| Illegal visit action | 409 from the locked write in the request; nothing is written or signalled |

Every appointment and visit transition is audited to Mongo with the same envelope Week 1
uses, actor being the requesting user for HTTP-initiated steps and `system:<workflow>` for
timer-initiated ones (no-show).

## 7. Testing

The cheapest tier that proves each property:

| Tier | Proves |
| --- | --- |
| T0 unit | Every allowed and forbidden transition in `state.py`; idempotency-key derivation |
| T1 contract | Routes, role gates, 200/202/403/404/409/503, fake Temporal client; a check-in still answers 200 when the post-commit signal fails |
| T2 workflow (time-skipping, mocked activities) | Confirm only after all steps; each failing step → `failed` and compensation called; conflict not retried; reschedule failure leaves original confirmed; no-show timer fires and returns the database's answer; a check-in signalled before a past deadline is kept; visit activities outlast a transient outage; `checked_in` / `cancel` end the visit workflow |
| T3 integration (compose Postgres) | Two concurrent `claim_slot` → exactly one wins and `version` incremented once; every R-2 row leaves slot and appointment agreeing; `create_pending` idempotent; past slot rejected; `promote_waitlist` under concurrency takes one entry (designed; realised as T2 `test_an_entry_already_promoted_is_skipped`); a check-in holding the appointment lock makes `mark_no_show` wait and answer `visit_exists`; strict visit actions; a real timer marks a no-show |
| T4 journey (compose Temporal + worker) | Book → confirmed → check-in → seen → complete; book → cancel → waitlisted patient confirmed (designed; realised as T3 with real Temporal and an in-process worker: `vst-004` and `wtl-001`) |
| Meta | Workflow determinism (sandbox-validate every registered workflow); single slot-status write path; the two declared edges (from `scheduling.activities` and `scheduling.router`) onto `patients.public` |

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

**Replay safety.** Changing `BookAppointmentWorkflow`'s command sequence breaks replay of
bookings already in flight when the new worker deploys. Slice 3's step 7 (starting
`visit-<appointment_id>`) is exactly such a change, so it must be gated with
`workflow.patched("booking-starts-visit")`: new executions take the new path, in-flight ones
replay the old one. The patch is deprecated (`workflow.deprecate_patch`) once no pre-slice-3
booking can still be running.

Every slice updates, in the same branch: `docs/PRD.md` (milestone status, traceability,
D-9…D-15, R-2…R-5 dispositions), `docs/ARCHITECTURE.md` §3 (moved from "design" to "built"
section by section — never ahead of the code), the README endpoint table, and `CLAUDE.md`'s
status and harness-requirement tally (requirements 1 and 3 become satisfied in slice 1).
