# Week 2 Slice 3 — Visit Workflow Implementation Plan

> **Superseded in part (2026-10-29).** Tasks 4, 5 and 7 below were built as written
> (`ee60bc9`, `e572237`, `b443ffa`) and then replaced after review by the synchronous
> design in spec D-12 as amended (§2, §4.2): the database records staff actions in the
> request's own transaction (200, not 202), and `VisitWorkflow` is only the durable
> no-show timer. The signal-with-start helper (`signal_visit`) and the signal-driven
> workflow described in those tasks no longer exist. The commits that implement the
> amended design: `a31e284` (the three-way no-show outcome), `b9ed7df` (actions written
> in the request, workflow reduced to its timer), `70cec30` (`REJECT_DUPLICATE` on the
> child start), `75cc2b2` (the grace validated once, in `Settings`), `2746aab` (the no-show
> proven on a real timer) and `400d093` (the spec amendment). The rest of this plan is
> left as the historical record; read the spec and the code for what was built.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a booking confirms, a durable `VisitWorkflow` carries the appointment through check-in → seen → complete, driven by staff signals, and marks it `no_show` by itself if nobody checks in by the slot's end plus a grace period.

**Architecture:** One `visit-<appointment_id>` workflow per appointment (spec D-12). Visit endpoints validate the transition against the database (synchronous 404/409) and then **signal-with-start** the workflow, so a booking confirmed before this slice — or one whose visit workflow has not started yet — is handled by the same call. Booking starts the visit as an **abandoned child** after `confirm`, behind `workflow.patched("booking-starts-visit")` so in-flight bookings still replay. Every visit write is an idempotent service function in the same paired-write discipline as booking (R-2: completion and no-show leave the slot `booked`).

**Tech Stack:** temporalio 1.34 (signals, `wait_condition` with timeout, child workflows, `patched`, `Replayer`), SQLAlchemy async, FastAPI.

**Spec:** `docs/superpowers/specs/2026-10-28-week2-scheduling-workflows-design.md` §2 (D-12), §4.2, §5, §8 (replay safety).

**Branch:** `feat/visit-workflow` (created from `main` at 80d4cbf).

---

## Facts the implementer needs (verified 2026-10-28 against temporalio 1.34)

1. **Signal-with-start:** `client.start_workflow(Wf.run, arg, id=..., task_queue=..., start_signal="check_in", start_signal_args=[...], id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)` starts the workflow if absent and delivers the signal; on a **running** id it just delivers the signal; on a **closed** id it raises `temporalio.exceptions.WorkflowAlreadyStartedError`. Verified.
2. **Timer:** `await workflow.wait_condition(pred, timeout=timedelta(...))` raises `TimeoutError` (builtin) when the timeout elapses first. In the time-skipping env a 2-hour wait completes instantly **while the test awaits the result**. Verified.
3. **Abandoned child:** `await workflow.start_child_workflow(Visit.run, arg, id=..., parent_close_policy=workflow.ParentClosePolicy.ABANDON)` returns once started; the child keeps `RUNNING` after the parent completes and can still be signalled. Verified.
4. **Child start on an id that is already running raises inside the parent workflow** (`WorkflowAlreadyStartedError`); uncaught, it fails the booking workflow *after* `confirm` committed. Catch it. Verified.
5. **Import spelling rule:** `from temporalio import workflow` only (no `from temporalio.workflow import ParentClosePolicy` — use `workflow.ParentClosePolicy`). `from temporalio.exceptions import ...`, `from temporalio.common import ...` are fine.
6. **Time-skipping hazard (learned in slice 2):** never leave a workflow on a task queue no worker polls — afterwards the test server stops skipping time for the whole session and long-timer tests hang. Every T2 test starts its worker before starting workflows and lets them finish (or signals them to finish) inside the worker context.
7. **Activity names** are global per task queue: name every new activity `visit.<method>`.
8. **The slot-write guard** forbids slot status writes outside `scheduling/service.py`; visit paths write no slot status at all (completed/no-show leave it `booked`).
9. Existing helpers to reuse: `activities.parse_id`, `SchedulingActivities._unit_of_work`, `booking.booking_state` style for RPC error mapping, `scheduling_helpers.seed_bookable` + purge convention, the T3 `booking_stack` fixture pattern, `token_for`, the T1 `FakeTemporal` pattern.

## File map

| File | Action | Responsibility |
| --- | --- | --- |
| `app/settings.py` | modify | `no_show_grace_minutes: int = 15` |
| `app/modules/scheduling/state.py` | modify | visit transitions; `VisitAction` |
| `app/modules/scheduling/service.py` | modify | `get_visit`, `require_visit_action`, `check_in`, `mark_seen`, `complete_visit`, `mark_no_show`, `no_show_deadline` |
| `app/modules/scheduling/contracts.py` | modify | `VisitRequest`, `VisitSignal` |
| `app/modules/scheduling/activities.py` | modify | `visit.*` activities; grace injected |
| `app/modules/scheduling/workflows.py` | modify | `VisitWorkflow`; booking starts it (patched) |
| `app/modules/scheduling/visits.py` | create | `signal_visit` (signal-with-start + error mapping), `visit_workflow_id` |
| `app/modules/scheduling/schemas.py`, `router.py` | modify | visit endpoints, `VisitRead` |
| `app/components.py`, `app/worker.py` | modify | register; pass grace |
| `tests/fixtures/histories/booking_pre_visit.json` | create | recorded history of the slice-2 booking workflow |
| tests (T0/T1/T2/T3, meta) | create | per task |
| `tests/cases/vst-*.yaml` | create | Stop-hook cases |
| docs | modify | Task 9 |

---

### Task 0: Record a pre-change booking history (do this FIRST)

The replay test in Task 6 needs a history produced by the **current** booking workflow. Once Task 6 changes the workflow it is too late to record one.

- [ ] Create `tests/tiers/t2_workflow/record_booking_history.py` (a script, not a test; `if __name__ == "__main__":` guard — the sandbox re-imports modules): start the time-skipping env, run `BookAppointmentWorkflow` to completion with recording fakes for the seven `booking.*` activities (copy the fake pattern from `test_booking_workflow.py`), fetch `await handle.fetch_history()`, and write `history.to_json()` to `tests/fixtures/histories/booking_pre_visit.json`. Run it once: `python -m tests.tiers.t2_workflow.record_booking_history`.
- [ ] Commit the script and the JSON: `test: record a slice-2 booking history for replay checks`.

### Task 1: Settings and the visit state machine

- [ ] `app/settings.py`: after the Temporal block, `no_show_grace_minutes: int = 15` with a comment (spec §4.2; grace after the slot's end before an unchecked-in appointment is a no-show). T0 test in `test_settings.py`: default 15.
- [ ] `app/modules/scheduling/state.py`: add

```python
class VisitAction(str, enum.Enum):
    CHECK_IN = "check_in"
    SEEN = "seen"
    COMPLETE = "complete"


#: What each staff action requires, stated as data like the appointment table above.
#: check_in: appointment confirmed and no visit yet; seen: visit checked_in;
#: complete: visit in_progress.
VISIT_PRECONDITIONS: dict[VisitAction, VisitStatus | None] = {
    VisitAction.CHECK_IN: None,
    VisitAction.SEEN: VisitStatus.CHECKED_IN,
    VisitAction.COMPLETE: VisitStatus.IN_PROGRESS,
}
VISIT_RESULT: dict[VisitAction, VisitStatus] = {
    VisitAction.CHECK_IN: VisitStatus.CHECKED_IN,
    VisitAction.SEEN: VisitStatus.IN_PROGRESS,
    VisitAction.COMPLETE: VisitStatus.COMPLETED,
}


def require_visit_action(
    action: VisitAction, appointment: AppointmentStatus, visit: VisitStatus | None
) -> None:
    """Raise Conflict unless `action` is legal now. Pure; the service supplies states."""
```

Implement it: the appointment must be `CONFIRMED` for every action (a cancelled or no-show appointment takes no visit actions; a `completed` one has nothing left to do); the visit status must equal `VISIT_PRECONDITIONS[action]`. The `Conflict` message names the action and both current states. T0 tests: enumerate every (action, appointment status, visit status-or-None) triple and assert legality exactly matches the three legal triples.

- [ ] Commit `feat: state the visit lifecycle as data`.

### Task 2: Visit service operations

**Files:** `app/modules/scheduling/service.py`, `tests/tiers/t3_integration/test_visit_service.py`.

Every function idempotent; a re-run after a committed first run writes nothing and audits nothing (same rule and docstring honesty as booking — R-1 caveat for a rolled-back commit). Audit entity `"visit"` for visit rows and the existing `"appointment"` for appointment status changes.

```python
async def get_visit(session, appointment_id, *, for_update=False) -> Visit | None
async def require_visit_action(session, *, appointment_id, action: VisitAction) -> None
    # NotFound if no appointment; else state.require_visit_action(action, appt.status, visit.status|None)
async def no_show_deadline(session, *, appointment_id, grace: timedelta) -> datetime
    # slot.ends_at + grace; NotFound if appointment or slot missing
async def check_in(session, audit, *, appointment_id, now, actor_user_id) -> None
async def mark_seen(session, audit, *, appointment_id, now, actor_user_id) -> None
async def complete_visit(session, audit, *, appointment_id, now, actor_user_id) -> None
async def mark_no_show(session, audit, *, appointment_id, actor_user_id) -> bool
```

Rules:
- `check_in`: lock the appointment (`get_appointment(for_update=True)`); if a visit already exists in `checked_in`/`in_progress`/`completed` → return (retry). Else `require_visit_action(CHECK_IN, ...)`; `INSERT visits (appointment_id, checked_in_at=now, status='checked_in') ON CONFLICT (appointment_id) DO NOTHING`; audit `checked_in` only if a row was inserted.
- `mark_seen`: lock appointment, load visit `for_update`; if visit status already `in_progress`/`completed` → return; else require `SEEN`; set `seen_at=now`, status `in_progress`; audit.
- `complete_visit`: lock appointment + visit; if visit `completed` and appointment `completed` → return; else require `COMPLETE`; visit `completed_at=now`, status `completed`; appointment → `completed` via `state.require_transition(CONFIRMED, COMPLETED)`. **No slot write** — the slot stays `booked` (spec §2 R-2 table). Audit both. One transaction (the caller's).
- `mark_no_show`: lock appointment; if status `no_show` → return True (retry); if a visit row exists or the status is not `confirmed` → return False (someone checked in, or it was cancelled — not a no-show); else appointment → `no_show` via `require_transition`, audit with actor (the workflow passes `system:visit-workflow`), return True. **No slot write.**

T3 tests (`test_visit_service.py`, reuse `seed_bookable` and confirm a booking through the service functions first — write a small `confirmed_appointment(factory)` helper in `scheduling_helpers.py`):
- check-in inserts one visit; a second check-in writes nothing (row count 1, one audit event).
- check-in on a pending/cancelled/no-show appointment → `Conflict`.
- seen before check-in → `Conflict`; seen twice → one write.
- complete before seen → `Conflict`; complete sets visit completed, appointment completed, `confirmed_at` untouched, **slot still `booked` with unchanged version**; complete twice → no second write.
- no-show with no visit → appointment `no_show`, returns True, slot still `booked`; again → True, no second audit; no-show after check-in → returns False and nothing changes; no-show on a cancelled appointment → False.
- `no_show_deadline` = slot `ends_at` + grace.
- `require_visit_action` maps missing appointment → `NotFound`.

Mutation check (scratch copy, as in slice 2): remove the "visit exists → False" guard in `mark_no_show`; remove the `CONFIRMED` requirement in `require_visit_action`; make `complete_visit` touch the slot. Each must fail a test. Report which.

- [ ] Commit `feat: add idempotent visit operations that leave the slot booked`.

### Task 3: Contracts and visit activities

- [ ] `contracts.py`:

```python
@dataclass(frozen=True)
class VisitRequest:
    appointment_id: str


@dataclass(frozen=True)
class VisitSignal:
    actor_user_id: str | None
```

- [ ] `activities.py`: `SchedulingActivities.__init__` gains `no_show_grace: timedelta = timedelta(minutes=15)` (keyword, defaulted so existing tests construct it unchanged). Add, each `@activity.defn(name="visit.<method>")`, each parsing ids with `parse_id` before the unit of work:
  - `visit.deadline(request: VisitRequest) -> str` — ISO string of `service.no_show_deadline(..., grace=self._no_show_grace)` (a string so the default converter round-trips it).
  - `visit.check_in(request, signal: VisitSignal)`, `visit.seen(...)`, `visit.complete(...)` — call the service with `now=self._clock.now()` and `actor_user_id=signal.actor_user_id`.
  - `visit.no_show(request) -> bool` — `actor_user_id="system:visit-workflow"`.
  Activities with two arguments are called with `args=[request, signal]` from the workflow.
- [ ] `components.py`: add the five methods to the registered list; `build_activities` gains a `no_show_grace: timedelta` parameter passed to `SchedulingActivities`; `app/worker.py` passes `timedelta(minutes=settings.no_show_grace_minutes)`. Update the T3 `booking_stack` fixture's `build_activities(...)` call accordingly.
- [ ] Run `tests/meta` (registry discovery must see the five new activities) and T0. Commit `feat: add visit activities`.

### Task 4: `VisitWorkflow`

**Files:** `app/modules/scheduling/workflows.py`, `tests/tiers/t2_workflow/test_visit_workflow.py`.

```python
@workflow.defn(name="Visit")
class VisitWorkflow:
    """One per confirmed appointment (spec D-12): check-in -> seen -> complete, driven by
    signals, or no_show by a durable timer. Signals only set flags; the run loop does
    every write through an activity, so a re-delivered signal changes nothing."""

    def __init__(self) -> None:
        self._signals: dict[str, VisitSignal] = {}
        self._cancelled = False

    @workflow.signal(name="check_in")
    def check_in(self, signal: VisitSignal) -> None:
        self._signals.setdefault("check_in", signal)

    # seen, complete: same shape. cancel: sets self._cancelled = True.

    @workflow.run
    async def run(self, request: VisitRequest) -> str:
        deadline = datetime.fromisoformat(await workflow.execute_activity_method(
            SchedulingActivities.deadline, request, **STEP))
        wait = max(deadline - workflow.now(), timedelta(0))
        try:
            await workflow.wait_condition(
                lambda: "check_in" in self._signals or self._cancelled, timeout=wait)
        except TimeoutError:
            if await workflow.execute_activity_method(
                    SchedulingActivities.no_show, request, **STEP):
                return "no_show"
            # Not a no-show: a check-in raced the timer and won in the database.
        if self._cancelled:
            return "cancelled"
        for step, activity in (("check_in", SchedulingActivities.check_in),
                               ("seen", SchedulingActivities.seen),
                               ("complete", SchedulingActivities.complete)):
            await workflow.wait_condition(lambda: step in self._signals or self._cancelled)
            if self._cancelled:
                return "cancelled"
            await workflow.execute_activity_method(
                activity, args=[request, self._signals[step]], **STEP)
        return "completed"
```

Fix the loop-variable capture in the lambda (`lambda s=step: ...`). For the race branch (timer fired, `no_show` returned False because a visit row exists), the `check_in` activity is idempotent, so running it with a synthetic `VisitSignal(actor_user_id=None)` if no signal arrived is safe — implement: `self._signals.setdefault("check_in", VisitSignal(actor_user_id=None))` before the loop in that branch.

Register in `components.py`. T2 tests (fakes for `visit.*`, recording calls; worker started first; fact 6):
- signals check_in, seen, complete (sent after start) → result `completed`; calls in order `deadline, check_in, seen, complete`.
- no signals, deadline 2 hours ahead → result `no_show` (time skips); `no_show` called once, no `check_in`.
- `no_show` fake returns False → the workflow proceeds; then signal seen/complete → `completed` with `check_in` called (the race branch).
- cancel signal before check-in → `cancelled`, no writes beyond `deadline`.
- a signal delivered twice → its activity called once.
- deadline already in the past → `no_show` immediately.
- sandbox meta test passes for `VisitWorkflow`.

Commit `feat: add the visit workflow with a durable no-show timer`.

### Task 5: Starting and signalling visits

**Files:** `app/modules/scheduling/visits.py`, `tests/tiers/t2_workflow/test_visit_signalling.py`.

```python
def visit_workflow_id(appointment_id) -> str: return f"visit-{appointment_id}"

async def signal_visit(client, *, appointment_id: uuid.UUID, action: VisitAction,
                       actor_user_id: str | None, task_queue: str) -> None:
    """Signal-with-start: delivers `action` to the visit workflow, starting it first if it
    is not running -- a booking confirmed before slice 3, or a check-in racing the
    booking's own start. A *closed* visit raises WorkflowAlreadyStartedError (REJECT_
    DUPLICATE), which means the visit already finished: Conflict, not a second visit."""
```

Map: `WorkflowAlreadyStartedError` → `Conflict("the visit for appointment … has already finished")`; `RPCError`/`RuntimeError` → `ServiceUnavailable` (same rationale comment as `booking.start_booking`). T2 tests: signal-with-start on a fresh id starts and delivers (check-in recorded); on a running id delivers without a second start; on a closed id → `Conflict`.

Commit `feat: signal visits with signal-with-start so every confirmed booking can be checked in`.

### Task 6: Booking starts the visit — replay-safely

- [ ] In `BookAppointmentWorkflow.run`, **after** the `try/except` (so a failure here never compensates a confirmed booking):

```python
        # Slice 3 added this command. `patched` keeps bookings that were already in
        # flight when it deployed replaying their original history (spec §8).
        if workflow.patched("booking-starts-visit"):
            try:
                await workflow.start_child_workflow(
                    VisitWorkflow.run, VisitRequest(request.appointment_id),
                    id=visit_workflow_id(request.appointment_id),
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                pass  # a visit endpoint signal-with-started it first
        return "confirmed"
```

`visit_workflow_id` must be importable inside the sandbox: define it in a pure module (e.g. `contracts.py`) and re-export from `visits.py`, rather than importing `visits.py` (which imports the client) into `workflows.py`.

- [ ] **Replay test** — `tests/tiers/t2_workflow/test_replay.py`:

```python
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

async def test_a_pre_visit_booking_history_still_replays():
    history = WorkflowHistory.from_json("booking-pre-visit", Path(...).read_text())
    await Replayer(workflows=[BookAppointmentWorkflow, VisitWorkflow]).replay_workflow(history)
```

Prove it bites: temporarily remove the `if workflow.patched(...)` guard (start the child unconditionally) → the replay test fails with a non-determinism error; restore. Report the error text.

- [ ] T2: the existing booking tests now also need a `Visit` workflow + `visit.*` fakes on their worker (the confirmed path starts a child). Add a test: a confirmed booking leaves `visit-<id>` running; a booking whose visit was already started (start it first via `signal_visit`) still returns `confirmed`; a failed booking starts no visit.
- [ ] Commit `feat: start the visit when a booking confirms, without breaking in-flight replays`.

### Task 7: HTTP endpoints

**Files:** `schemas.py` (`VisitRead`: appointment_id, status, checked_in_at, seen_at, completed_at; `VisitAccepted`: appointment_id, action), `router.py`, `tests/tiers/t1_contract/test_visits_contract.py`, `tests/tiers/t3_integration/test_visit_round_trip.py`.

| Endpoint | Roles | Behaviour |
| --- | --- | --- |
| `POST /appointments/{id}/check-in` | front_desk, admin | `service.require_visit_action(CHECK_IN)` → 404/409; then `signal_visit` → **202** `VisitAccepted` |
| `POST /appointments/{id}/seen` | provider, admin | same with SEEN |
| `POST /appointments/{id}/complete` | provider, admin | same with COMPLETE |
| `GET /appointments/{id}/visit` | owning patient, provider, front_desk, admin | `VisitRead`, 404 if no visit; a patient gets 404 for another patient's appointment (same rule as GET appointment) |

202, not 200: the write happens in the workflow. The precheck makes an illegal action a synchronous 409; the activity re-checks, so a race between precheck and signal is harmless.

T1: role gates for each, 409 mapping when the precheck raises (override the session dependency as the slice-2 contract tests do, or monkeypatch `service.require_visit_action`), 503 on Temporal down, 202 body. T3 round trip (in-process worker, `booking_stack` pattern, grace irrelevant): book → confirmed → `POST check-in` → poll `GET .../visit` until `checked_in` → `seen` → `complete` → appointment `completed`, visit timestamps set, slot `booked`; `complete` before `seen` → 409; check-in twice → second is 409 (precheck sees the visit). Audit trail for the visit.

Commit `feat: expose visit actions over HTTP`.

### Task 8: Verify and cases

- Full lanes, ruff, route_check, `tests/meta`. Container demo: rebuild with `--build`, book via API, check-in/seen/complete via API, read the visit; `temporal workflow list` shows `visit-<id>` Completed. Record it.
- Cases (verify requirement ids in `docs/PRD.md`): `vst-001-a-visit-runs-check-in-to-complete` (workflow, P0, `PART-A-FR-3`, the T2 happy-path test); `vst-002-no-check-in-becomes-a-no-show` (workflow, P0, `PART-A-FR-3`, the no-show T2 test); `vst-003-pre-visit-bookings-still-replay` (workflow, P0, `PART-A-NFR-2`, the replay test); `vst-004-visit-end-to-end` (integration, P1, `PART-A-FR-3`, the T3 round trip). Commit `test: add cases for the visit workflow`.

### Task 9: Docs

Verify every claim against code. PRD: FR-3 → built (visit lifecycle; billing/follow-up on completion remain Week 3 stand-ins — none were added here, say so); D-12 added to §8; R-2 status → visit paths done (complete/no-show leave slot booked), cancel/reschedule pending; known gap: provider per-object authorisation (any provider may mark any visit seen/complete — spec §1); known gap: bookings confirmed before slice 3 get a visit workflow only when first signalled, so they are never auto-no-showed. ARCHITECTURE: §3 visit flow built with file:line refs, the signal-with-start rationale, replay safety (`patched` + the recorded-history test). README endpoint table + a short visit demo. CLAUDE.md status/test counts. Commit `docs: record the visit workflow`.

### Task 10: Land

Re-verify; fast-forward `main` and push **only with the user's go-ahead**.
