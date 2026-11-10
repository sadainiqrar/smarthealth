# Week 3 slice 4 — Kafka → Temporal, billing and the payment gateway: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Domain events start the workflows that follow them — closing PRD R-17 (lost cancel
follow-ups) and R-25 (a deactivated provider's existing appointments) and adding the
post-visit charge (FR-3) — and the three billing stand-ins become real calls to a fake payment
gateway with idempotency keys, recorded in a `billing` module.

**Spec:** `docs/superpowers/specs/2026-11-10-weeks3-5-architecture-design.md` §3.5, §5.3, D-20,
D-23, D-27.

## Decisions taken while planning

1. **One billing row per appointment**, a state machine rather than a ledger:
   `authorized → captured → refunded`, `authorized → voided`, or `declined`. Every operation
   is idempotent by that state and by the gateway idempotency key `<op>-<appointment_id>`,
   so an activity retried after the gateway answered but before the row was written gets the
   same answer again and writes it.
2. **What each stand-in becomes.** Pre-check = authorise (a hold). Compensation
   (`fail_booking`) and the cancel follow-up's refund = *release*: void an authorisation,
   refund a capture, nothing otherwise. Post-visit = capture. A reschedule releases the old
   appointment's authorisation inside `reschedule.complete`. All through
   `app/modules/billing/public.py` — no workflow code changes, so in-flight histories replay.
3. **The gateway is `services/payments_mock`**, its own container standing in for an
   external provider: in-memory, honours `Idempotency-Key`, and misbehaves on request —
   latency, 503s, timeouts *after* the charge is taken, declines for named customers
   (`POST /_chaos`). Part A puts a circuit breaker in front of it (`app/core/circuit.py`).
4. **Consumers get a context** (`ConsumerContext`: the Temporal client and settings) and run
   in their own process, `python -m app.consumers` (compose `consumers`). Each starts a
   workflow under a deterministic id; `WorkflowAlreadyStartedError` is success (D-20).
5. **Deactivation reacts to every `providers.state` with `is_active = false`**, not only to a
   transition: the consumer keeps no copy of the previous state, and a repeat (a republish)
   starts a workflow that finds nothing left to cancel. Id
   `provider-deactivation-<provider>-<event>`.
6. **`PostVisitWorkflow` only captures in this slice**; the follow-up reminder joins it in
   slice 5 behind `workflow.patched`.

## Tasks

- [ ] 1. `services/payments_mock`: app, Dockerfile, compose service (no profile; test stack
  port 18090); T1 tests for idempotency, chaos, decline.
- [ ] 2. `app/core/circuit.py` + T0 tests.
- [ ] 3. `app/modules/billing/`: model, migration 0006, gateway client, service
  (authorize/capture/release), `public.py`; `billing.status_updated` schema and topic; T3
  against the mock, including a timeout after the charge.
- [ ] 4. Activities: precheck, `fail_booking`, refund, `reschedule.complete` through
  `billing.public`; new `post_visit.charge`, `provider.future_appointments`,
  `provider.cancel_appointment`; `PostVisitWorkflow`, `ProviderDeactivationWorkflow`;
  registration; T2 workflow tests.
- [ ] 5. Consumer runtime takes a context; `app/modules/scheduling/consumers.py` (three
  consumers); `app/consumers.py`; compose `consumers`; the router stops starting the cancel
  follow-up; meta-test `test_consumers_idempotent.py`.
- [ ] 6. T3 end to end: HTTP cancel → outbox → relay → Kafka → consumer → follow-up →
  refund row; deactivation cancels future appointments; completion captures.
- [ ] 7. Cases, docs (R-17 and R-25 closed, FR-3 billing), merge.
