# Week 2 Slice 4 — Cancel, Reschedule and Waitlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cancellation and reschedule as first-class flows (CLAUDE.md): cancel releases the slot in the same transaction as the appointment change, stops the visit timer, runs the refund stand-in and promotes a waitlisted patient onto the freed slot; reschedule books the new slot **before** releasing the old one (D-14).

**Architecture — applying slice 3's lesson:** a human action whose outcome the caller needs is a synchronous database write; follow-ups that must happen eventually are a workflow.

- **Cancel** = synchronous paired write in the request (`appointments.status = cancelled` + `provider_slots.status = free`, one transaction, under the appointment lock) answering **200**, then a best-effort start of `CancelFollowUpWorkflow` (`cancel-<id>`): signal the visit timer, refund stand-in, waitlist promotion.
- **Reschedule** is inherently a workflow — it must book first — so it answers **202**. `RescheduleWorkflow` runs `BookAppointmentWorkflow` as a child for the new appointment (`rescheduled_from_id` set); only if that confirms does it move the old appointment to `rescheduled` and free its slot (paired write), then the same follow-ups.
- **Waitlist promotion** is shared workflow code: candidates in order, each tried as a child booking with id `booking-wl-<entry_id>` and `ALLOW_DUPLICATE_FAILED_ONLY` (a failed attempt may be retried later; a confirmed one never twice); the first that confirms is marked `fulfilled`.

`BookAppointmentWorkflow` is **not** changed: its request gains an optional `rescheduled_from_id` field (defaulted, so recorded histories still decode) carried into `create_pending`.

**Tech Stack:** temporalio 1.34 (child workflows, external signals), SQLAlchemy async, FastAPI.

**Spec:** `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §2 (R-2 table, D-13, D-14), §4.3–4.5. This plan amends §4.3 (cancel is synchronous, as visit actions became in slice 3) — Task 0 records that.

**Branch:** `feat/cancel-reschedule` (from `main` at 2996f91).

---

## Facts the implementer needs (verified 2026-10-29 against temporalio 1.34)

1. `await workflow.get_external_workflow_handle(id).signal("cancel")` on a **missing** workflow raises `temporalio.exceptions.ApplicationError` inside the workflow — catch it (the visit may not exist: booking pre-slice-3, or already finished). On an existing one it delivers. Verified.
2. A failed child surfaces as `temporalio.exceptions.ChildWorkflowError` (`.cause` is the child's `ApplicationError`). Verified.
3. `id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY`: a child id whose previous run **failed** can start again; one whose run **completed** raises `WorkflowAlreadyStartedError` in the parent. A **running** id raises `WorkflowAlreadyStartedError` too. Verified.
4. **Time-skipping pitfalls** (`tests/README.md`): a parent that starts a child and closes stops time skipping for the rest of that test server — every T2 test here involves children, so use the function-scoped `isolated_env`; never leave a workflow on an unpolled queue; fake deadlines from `await env.get_current_time()`. Bookings in these tests confirm, which starts a visit child — reuse `tests/tiers/t2_workflow/visit_fakes.py`.
5. Import spelling: `from temporalio import workflow|activity` only; `workflow.ParentClosePolicy`; `from temporalio.common import ...`, `from temporalio.exceptions import ...` are fine.
6. Slot status is written only in `scheduling/service.py` (`tests/meta/test_slot_writes.py`). The new paired writes go there.
7. Activity names are global: `cancel.*`, `reschedule.*`, `waitlist.*`.
8. The visit timer already handles cancellation twice over: a `cancel` signal ends it `"cancelled"`; a lost signal → at the deadline `mark_no_show` sees a non-confirmed appointment → `"closed"`.

## File map

| File | Action |
| --- | --- |
| spec | modify (Task 0) |
| `app/modules/scheduling/state.py` | modify: `require_cancellable` |
| `app/modules/scheduling/service.py` | modify: `cancel_appointment`, `complete_reschedule`, `create_pending(rescheduled_from_id)`, waitlist ops |
| `app/modules/scheduling/contracts.py` | modify: `BookingRequest.rescheduled_from_id`, `FollowUpRequest`, `RescheduleRequest` |
| `app/modules/scheduling/activities.py` | modify: new activities |
| `app/modules/scheduling/workflows.py` | modify: `CancelFollowUpWorkflow`, `RescheduleWorkflow`, `_promote_waitlist` |
| `app/modules/scheduling/followups.py` | create: start helpers (best-effort follow-up start; reschedule start) |
| `app/modules/scheduling/schemas.py`, `router.py` | modify: cancel, reschedule, waitlist endpoints |
| `app/components.py` | modify: register |
| tests T0–T3, cases | create |
| docs | modify |

---

### Task 0: Record the design amendment

- [ ] In the spec, rewrite §4.3 to: cancel is a synchronous paired write answering 200 (same reasoning as amended D-12 — the caller needs a definitive answer, and a 202 followed by an async write races with the visit timer and with a concurrent booking just as visit check-ins did); follow-ups run in `CancelFollowUpWorkflow`, started best-effort after commit; if that start fails the follow-ups are lost — record as a known gap closed by Week 3's transactional outbox. §4.4: reschedule as above, noting the old appointment must still be `confirmed` with no visit when the child confirms, else the reschedule ends `superseded` and the new booking stands. §4.5: promotion via ordered candidates + child ids with `ALLOW_DUPLICATE_FAILED_ONLY` (replacing the `FOR UPDATE SKIP LOCKED` design, which cannot hold a lock across a child workflow). Add a D-16 row for "cancel is synchronous". Commit `docs: make cancel synchronous and define waitlist promotion by workflow id`.

### Task 1: State and the paired writes

**`state.py`:** `require_cancellable(status: AppointmentStatus, has_visit: bool) -> None` — `Conflict` unless status is `pending` or `confirmed` **and** no visit row exists (a checked-in patient is not cancelled or rescheduled; the visit completes or the slot is otherwise handled). T0: enumerate statuses × has_visit.

**`service.py`** (follow existing conventions: lock via `get_appointment(for_update=True)`, `_snapshot`/`_audit`, `require_transition`):

```python
async def cancel_appointment(session, audit, *, appointment_id, reason, now, actor_user_id) -> Appointment
```
- Lock; `NotFound`; `require_cancellable(status, visit exists)`; if the appointment holds a slot (`slot_id` set) release it: `UPDATE provider_slots SET status='free', version=version+1 WHERE id=:slot AND status IN ('held','booked')`; appointment → `cancelled` via `require_transition`, `cancelled_at=now`, `cancellation_reason=reason`; audit. **Strict** (HTTP path): cancelling an already-cancelled appointment is `Conflict`.
- A `pending` appointment mid-booking: its slot may be `held` (released here) or not yet claimed (`slot_id` None — nothing to release). The booking's next step then finds it not `pending` → permanent `Conflict` → `fail_booking` no-ops (not pending). Spec §4.3 already describes this race.

```python
async def complete_reschedule(session, audit, *, old_appointment_id, actor_user_id) -> Literal["rescheduled", "superseded"]
```
- Lock the old appointment; if already `rescheduled` → `"rescheduled"` (idempotent, activity retry); if not `confirmed` or a visit exists → `"superseded"` (cancelled or checked in meanwhile — the new booking stands, nothing to release); else release its slot (`booked` → `free`, version+1) and `confirmed → rescheduled`; audit.

```python
async def create_pending(..., rescheduled_from_id: uuid.UUID | None = None)   # pass through to the INSERT
```

**Waitlist ops:**
```python
async def add_waitlist_entry(session, audit, *, patient_id, provider_id, department_id, window_start, window_end, actor_user_id) -> WaitlistEntry
    # Unprocessable if window_end <= window_start
async def list_waitlist(session, *, patient_id, page) -> tuple[list[WaitlistEntry], int]
async def waitlist_candidates(session, *, slot_id, limit=3) -> list[tuple[uuid.UUID, uuid.UUID]]
    # (entry_id, patient_id) of active entries whose window contains slot.starts_at and whose
    # provider_id/department_id, when set, match the slot's; oldest created_at first.
    # Excludes the patient who held the slot being freed? No -- they cancelled; leave it to the window.
async def fulfil_waitlist_entry(session, audit, *, entry_id, actor_user_id) -> None
    # active -> fulfilled; idempotent if already fulfilled
```

T3 tests (`test_cancel_service.py`, `test_waitlist_service.py`): cancel a confirmed appointment → appointment `cancelled` + slot `free`, version +1, one transaction (assert both); cancel a pending held-slot appointment → slot `free`; cancel a pending unclaimed one → no slot write; cancel twice → `Conflict`; cancel with a visit → `Conflict`; cancel completed/no-show/failed → `Conflict`. `complete_reschedule`: confirmed → rescheduled + slot free; again → `"rescheduled"`, no second write; on cancelled → `"superseded"`, nothing written. Waitlist: candidates honour window, provider/department filters, status active, order; fulfil idempotent; invalid window → `Unprocessable`. **R-2 check:** after cancel and after reschedule, the old slot is `free` and appears in `service.list_free_slots`.

Mutation proofs (scratch copy / careful revert): cancel without releasing the slot; `complete_reschedule` ignoring a visit; candidates ignoring `status='active'`. Each must fail a test.

Commit `feat: cancel and reschedule as paired writes, and the waitlist operations`.

### Task 2: Contracts and activities

`contracts.py`:
```python
@dataclass(frozen=True)
class BookingRequest:  # add, last, defaulted:
    rescheduled_from_id: str | None = None

@dataclass(frozen=True)
class FollowUpRequest:
    appointment_id: str     # the cancelled (or rescheduled-away) appointment
    slot_id: str | None     # the slot it released, if any
    actor_user_id: str | None

@dataclass(frozen=True)
class RescheduleRequest:
    old_appointment_id: str
    new_appointment_id: str
    patient_id: str
    new_slot_id: str
    actor_user_id: str | None
    reason: str | None = None
```
Verify `BookingRequest` with the new field still decodes `tests/fixtures/histories/booking_pre_visit.json` (run the replay test).

Activities (`parse_id` before the unit of work; unlimited retry for all of these — they are follow-ups that must eventually happen and every one is idempotent):
- `booking.create_pending` passes `rescheduled_from_id` through.
- `cancel.refund(FollowUpRequest)` — stand-in: structured log line, like `billing_precheck`.
- `reschedule.complete(RescheduleRequest) -> str` — `service.complete_reschedule`.
- `waitlist.candidates(slot_id: str) -> list[list[str]]` — `[[entry_id, patient_id], ...]` (JSON-friendly).
- `waitlist.fulfil(entry_id: str)`.
Register in `components.py`. Commit `feat: add cancel, reschedule and waitlist activities`.

### Task 3: Workflows

In `workflows.py`:

```python
FOLLOW_UP = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": UNLIMITED_RETRY}


async def _stop_visit(appointment_id: str) -> None:
    """Best-effort: the visit timer also ends by itself ('closed') if this is lost."""
    try:
        await workflow.get_external_workflow_handle(visit_workflow_id(appointment_id)).signal("cancel")
    except ApplicationError:
        pass  # no visit workflow (pre-slice-3 booking, or it already finished)


async def _promote_waitlist(slot_id: str) -> str | None:
    """Offer a released slot to waitlisted patients, oldest first. Returns the appointment
    id that confirmed, or None. Each attempt is a child booking with id booking-wl-<entry>:
    ALLOW_DUPLICATE_FAILED_ONLY lets a failed attempt be retried by a later release, and
    makes a confirmed one impossible to repeat."""
    for entry_id, patient_id in await workflow.execute_activity_method(
            SchedulingActivities.waitlist_candidates, slot_id, **FOLLOW_UP):
        appointment_id = str(workflow.uuid4())
        try:
            await workflow.execute_child_workflow(
                BookAppointmentWorkflow.run,
                BookingRequest(appointment_id, patient_id, slot_id, actor_user_id="system:waitlist"),
                id=f"booking-wl-{entry_id}",
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except (WorkflowAlreadyStartedError, ChildWorkflowError):
            continue  # entry already being promoted, or this booking failed: try the next
        await workflow.execute_activity_method(
            SchedulingActivities.waitlist_fulfil, entry_id, **FOLLOW_UP)
        return appointment_id
    return None


@workflow.defn(name="CancelFollowUp")
class CancelFollowUpWorkflow:
    @workflow.run
    async def run(self, request: FollowUpRequest) -> str:
        await _stop_visit(request.appointment_id)
        await workflow.execute_activity_method(SchedulingActivities.refund, request, **FOLLOW_UP)
        if request.slot_id is None:
            return "no-slot"
        promoted = await _promote_waitlist(request.slot_id)
        return f"promoted:{promoted}" if promoted else "released"


@workflow.defn(name="Reschedule")
class RescheduleWorkflow:
    @workflow.run
    async def run(self, request: RescheduleRequest) -> str:
        try:
            await workflow.execute_child_workflow(
                BookAppointmentWorkflow.run,
                BookingRequest(request.new_appointment_id, request.patient_id, request.new_slot_id,
                               request.actor_user_id, request.reason,
                               rescheduled_from_id=request.old_appointment_id),
                id=booking_workflow_id(request.new_appointment_id),   # pure helper
            )
        except ChildWorkflowError:
            return "failed"          # new booking failed and compensated; old untouched (D-14)
        outcome = await workflow.execute_activity_method(
            SchedulingActivities.reschedule_complete, request, **FOLLOW_UP)
        if outcome == "superseded":
            return "superseded"
        await _stop_visit(request.old_appointment_id)
        old_slot = ...  # the slot the old appointment held: return it from reschedule.complete
                        # (make the activity return {"outcome", "released_slot_id"})
        await _promote_waitlist(old_slot)
        return "rescheduled"
```
Fix the sketch: `workflow.uuid4()` is the deterministic UUID inside workflows — confirm it exists in 1.34 (`workflow.uuid4`); `booking_workflow_id` must be importable in the sandbox (move it to `contracts.py` like `visit_workflow_id`, re-export from `booking.py`); `reschedule.complete` returns the released slot id. Activity method names: `refund`, `reschedule_complete`, `waitlist_candidates`, `waitlist_fulfil` with `@activity.defn(name="cancel.refund")` etc.

Register both workflows. T2 tests (`isolated_env`, fakes for every activity incl. `booking.*` and `visit.*`):
- Cancel follow-up: signals the visit (start a fake visit workflow first; assert it ended `"cancelled"`); no visit → still completes; refund called; no candidates → `"released"`; a candidate whose booking confirms → `"promoted:<id>"` and `waitlist.fulfil` called with that entry; first candidate's booking fails (fake `claim_slot` raises Conflict for it) → second candidate promoted; an entry already promoted (its `booking-wl-` id completed) → skipped.
- Reschedule: child confirms → `reschedule.complete` called after it, visit signalled, promotion run → `"rescheduled"`; child fails → `"failed"`, `reschedule.complete` never called; `reschedule.complete` returns superseded → `"superseded"`, no promotion.
- Sandbox meta test passes for both workflows; the booking replay test still passes.

Commit `feat: add the cancel follow-up and reschedule workflows`.

### Task 4: Starting them, and HTTP

`followups.py`:
- `start_cancel_follow_up(client, request, *, task_queue)` — id `cancel-<appointment_id>`, `REJECT_DUPLICATE`; any Temporal failure is **logged and swallowed** (the cancel already committed; known gap, Week 3 outbox) with a bound like `visits.NOTIFY_TIMEOUT`.
- `start_reschedule(client, request, *, task_queue)` — id `reschedule-<new_appointment_id>`, `USE_EXISTING` + `REJECT_DUPLICATE`; Temporal failure → `ServiceUnavailable` (nothing was written yet, so the client must retry).

Endpoints (`router.py`; patient ownership via `_own_patient_id`, 404 for someone else's appointment as elsewhere):

| Endpoint | Roles | Behaviour |
| --- | --- | --- |
| `POST /appointments/{id}/cancel` `{reason}` | owning patient, front_desk, admin | `service.cancel_appointment` → commit → `start_cancel_follow_up` → **200** `AppointmentRead` |
| `POST /appointments/{id}/reschedule` `{new_slot_id, reason?}` + optional `Idempotency-Key` | owning patient, front_desk, admin | synchronous precheck (old exists, owned, `confirmed`, no visit — reuse `require_cancellable` on a fresh read) → 404/403/409; new appointment id via `appointment_id_for(caller, key)`; `start_reschedule` → **202** `{appointment_id: <new>, rescheduled_from: <old>}` |
| `POST /waitlist` `{patient_id, provider_id?, department_id?, window_start, window_end}` | patient (own only), front_desk, admin | 201 `WaitlistRead`; `AwareDatetime` bounds |
| `GET /waitlist?patient_id=` | patient (own; patient_id forced to theirs), front_desk, admin, provider | `Page[WaitlistRead]` |

T1: role gates, 409/404 mappings, 503 for reschedule start failure, 200 for cancel even when the follow-up start fails (fake raising), validation (naive datetimes, `window_end <= window_start` → 422). T3 round trips (in-process worker with real activities, `booking_stack`-style fixture registering every workflow):
- book → confirmed → cancel 200 → appointment `cancelled`, slot `free`; `cancel-<id>` completes; visit workflow ended `cancelled` (or `closed`); audit trail.
- waitlist: patient B joins the waitlist covering the slot; A books it, then cancels → follow-up promotes B → B has a `confirmed` appointment on that slot, entry `fulfilled`.
- reschedule: book slot S1 → reschedule to S2 → poll new appointment `confirmed`, old `rescheduled` with S1 `free`, new `rescheduled_from_id == old`; reschedule to a taken slot → new `failed`, old still `confirmed`, S1 still `booked`.
Commit `feat: expose cancel, reschedule and the waitlist over HTTP`.

### Task 5: Verify, demo, cases

All lanes, ruff, route_check, the T2 tier 3× (report durations/flakes). Container demo on the dev stack (`--build`; `FRONTEND_PORT=5174` if needed): book → cancel → slot free; waitlist promotion; reschedule. `temporal workflow list` shows `cancel-…`, `reschedule-…`, `booking-wl-…`. Cases (verify ids in PRD): cancel paired write (integration, P0, `PART-A-FR-2`), reschedule books before releasing (workflow, P0, `PART-A-FR-2`), failed reschedule leaves the original (workflow, P0, `PART-A-NFR-2`), waitlist promotion end to end (integration, P1, `PART-A-FR-2`). `route_check --write-catalog`. Commit `test: add cases for cancel, reschedule and the waitlist`.

### Task 6: Docs, then review and land

PRD (FR-2 → built except billing/notification stand-ins; R-2 → closed for all paths: confirm, compensate, cancel, reschedule, complete, no-show; D-13, D-14, D-16 in §8; new gap: cancel follow-ups lost if their start fails, closed by Week 3 outbox; recount §11), ARCHITECTURE §3.3 built (file:line, diagrams), README endpoints + a cancel/reschedule/waitlist demo, CLAUDE.md status + counts. Every claim verified against code. Then an independent review, fixes, and land on `main` only with the user's go-ahead.
