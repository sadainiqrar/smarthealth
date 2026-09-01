# SmartHealth Testing Harness — Design

**Date:** 2026-09-01
**Status:** Approved (design); implementation plan pending
**Prior art:** `aera-assistant-client` e2e harness (Playwright tiers + YAML case catalog +
LLM-as-judge + Claude Code skills + `Stop` hook)

---

## 1. Why this exists, and why now

SmartHealth is graded on **architecture clarity, reliability/failure handling, and
observability maturity** — not feature count. Those three are precisely the properties that
cannot be demonstrated by reading code; they have to be *asserted*. A harness built after the
features is a regression net. A harness built before them is a design driver: it forces the
application to expose the seams (configurable topic prefixes, swappable providers, enumerable
registries, injectable clock) that make a distributed system testable at all.

The repository currently contains only requirements. Building the spine now costs one focused
effort and makes every subsequent week cheaper. Building it in Week 4 means retrofitting seams
into five weeks of code.

**Decision (locked):** full spine now, thin content. All machinery lands up front; real cases
accrete week by week.

## 2. Goals

1. Prove the invariants the assignment names explicitly — appointment confirms only after the
   whole workflow succeeds; partial failure corrupts nothing; every async path is idempotent;
   background work never blocks user-facing flows.
2. Make observability a **tested** property, not a claimed one: spans exist, trace context
   survives the Kafka and Celery hops, metrics move.
3. Give a coding agent (Claude Code, in future sessions) a low-friction path to author a test
   as a side effect of implementing a feature, and a deterministic gate that makes skipping
   that path a conscious, logged choice.
4. Provide traceability from PRD requirement → test case → result, which is itself a graded
   deliverable.

## 3. Non-goals

- Not a coverage target. Coverage is not on the rubric.
- Not a ported catalog. Aera had 584 pre-existing manual cases and needed pure-data cases to
  absorb them. We have none; the DSL exists for metadata uniformity, not volume.
- Not a graded deliverable in itself. If the harness starts consuming week-scope, the harness
  loses. It serves the rubric; it is not the rubric.
- No browser testing. There is no UI in scope.

## 4. What transfers from the Aera harness, and what does not

| Aera element | Verdict | Reasoning |
|---|---|---|
| Tiered lanes, cheapest-first | **Transfers** | Same economics; different tools |
| YAML case catalog as the authoring surface | **Transfers, adapted** | Kept for metadata/traceability/judge-prompt uniformity, with a code escape hatch |
| Deterministic fixture mode for the LLM | **Transfers wholesale** | Part B has the identical non-determinism problem |
| LLM-as-judge, non-gating, with a calibration log | **Transfers wholesale** | Including the discipline that a verdict never gates CI until calibration earns it |
| Agent skills + a `Stop` hook as enforcement | **Transfers, reimplemented in Python** | Same contract; bash is a liability on Windows |
| Playwright, component tests, visual regression | **Dropped** | No UI |
| Per-worker browser identity isolation | **Replaced** | The isolation problem moves to DB schemas, topics, and task queues |
| Routing dry-run as the hook's validation authority | **Transfers** | Deterministic, fast, infra-free — exactly what a gate needs |

The single most important inherited lesson is Aera's **82%-stub trap**: a routed case with an
empty journey and no checks is silently skipped, so the catalog *looks* automated while
asserting nothing. Our schema makes that a hard validation error (§7.3).

## 5. Tier model

Five tiers, each defined by what it can actually prove and what it costs.

| Tier | Infrastructure | Proves | Typical cost |
|---|---|---|---|
| **T0 — unit** | none | domain logic, state-machine transitions, validators, pure policy | ms |
| **T1 — contract** | none; `httpx.ASGITransport` over the FastAPI app, dependencies overridden | API surface and status codes, authz per role, OpenAPI snapshot, Kafka schema compatibility against Schema Registry rules | ms |
| **T2 — workflow** | Temporal SDK `WorkflowEnvironment.start_time_skipping()`, activities mocked | booking/visit workflow correctness, compensation on partial failure, timer-driven flows, workflow replay-safety | ~1s |
| **T3 — integration** | compose `test` profile (real Postgres, Mongo, Redis, RabbitMQ, Kafka + Schema Registry) | repositories, Kafka consumers, Celery tasks, outbox, event round-trip, idempotency under genuine duplicate delivery | ~10s |
| **T4 — journey + chaos** | full stack including real workers and real Temporal | multi-step business journeys, deliberate failure injection, trace and metric assertions, post-condition invariants | ~1min |

**T2 is the highest-value tier for this rubric.** The assignment's headline invariant —
"appointment marked confirmed only after successful processing; partial failures must not
corrupt the scheduling state" — is a workflow-layer property, and the SDK's time-skipping
environment proves it deterministically in about a second, with no containers. Reminder,
no-show, and follow-up flows involve multi-hour timers that fire instantly there.

### 5.1 Why Temporal is not containerized below T4

Containerizing Temporal to test workflows forces a choice between sleeping for real and not
testing timers at all. The Python SDK's test environment removes that choice. A real Temporal
container appears only in T4, where the point is the whole stack behaving together.

## 6. Test infrastructure

### 6.1 Decision: compose `test` profile, not testcontainers

**Chosen:** a `test` profile in the project's compose topology, on offset ports and separate
volumes, so a dev stack and a test stack coexist.

Rationale:

1. **One topology, one source of truth.** "Docker setup operational" is a Week-1 deliverable
   and the mentor will run the compose file. Testcontainers defines the topology a second
   time, and drift between the two produces the worst failure mode available: green tests,
   broken `docker compose up`.
2. **Boot cost on Windows Docker Desktop.** Postgres + Mongo + Redis + RabbitMQ + Kafka +
   Schema Registry is 60–120s cold. Testcontainers pays that per session; compose pays it once
   and amortises it across a five-week dev loop.
3. **Chaos needs addressable containers.** `docker compose kill kafka`, `pause celery-worker`,
   restart mid-workflow. Reachable with testcontainers only by rebuilding compose's addressing.

Testcontainers' genuine advantage is isolation. That advantage is obtained a different way
(§6.3), and the fixture API is shaped so the backend can be swapped later without touching a
single test.

Port offsets (test profile): Postgres 15432, Mongo 27018, Redis 16379, RabbitMQ 5673 / mgmt
15673, Kafka 19092, Schema Registry 18081, Temporal 17233. Volumes suffixed `-test`.

### 6.2 Stack lifecycle

`tests/harness/stack.py` exposes a session-scoped `stack` fixture:

- `stack.up()` — `docker compose --profile test up -d`, then block on real readiness probes
  (Postgres `SELECT 1`, Kafka metadata fetch, Schema Registry `/subjects`, Temporal namespace
  describe, RabbitMQ mgmt API, Mongo ping, Redis ping). Never `sleep`-based.
- `stack.reset()` — truncate/drop per-run artifacts between sessions.
- `stack.service(name)` — a handle exposing `kill()`, `pause()`, `unpause()`, `restart()` for
  the chaos tier.
- Reuses an already-running stack if healthy (`SMARTHEALTH_TEST_STACK=reuse`, the default), so
  the dev loop pays boot cost once.

The fixture's surface is deliberately testcontainers-shaped. Swapping the backend is a change
to this one module.

### 6.3 Isolation layer

`tests/harness/isolation.py` gives each test session (and each `pytest-xdist` worker) a private
slice of a shared stack:

| Resource | Isolation mechanism |
|---|---|
| Postgres | per-session database cloned from a migrated template DB (fast; migrations run once) |
| MongoDB | per-session database `sh_test_<runid>` |
| Redis | per-worker logical DB index plus a `runid:` key prefix |
| Kafka | topic prefix `t_<runid>_`, consumer group `g_<runid>` |
| RabbitMQ | per-run vhost |
| Temporal | per-run task-queue prefix (and namespace at T4) |

This is where testcontainers' isolation benefit is recovered, at roughly 150 lines and without
a second topology definition.

### 6.4 App-side affordances the harness requires

The isolation and determinism above are not free — they constrain the application's
configuration surface. These are harness requirements on Week-1 scaffolding, and each is good
practice independently:

1. Every topic, queue, and task-queue name derives from settings and honours a configurable
   prefix. No string literals at the call site.
2. LLM and embedding clients are built by a **provider factory** keyed on settings, so a fake
   can be substituted without patching internals.
3. Kafka consumers, Temporal workflows, and Celery tasks are registered in enumerable
   **registries**, so the meta-tests in §8 can discover them at runtime.
4. Consumers accept an explicit idempotency key surface (event id / dedupe key), rather than
   inferring uniqueness ad hoc.
5. The OpenTelemetry tracer provider is swappable, so tests can install an in-memory span
   exporter.
6. Time is obtained through an injectable `now()` provider, never `datetime.utcnow()` inline.
7. Every service exposes a readiness endpoint the stack fixture can probe.

## 7. Case catalog

### 7.1 Shape

Cases live at `tests/cases/<id>.yaml`. The YAML carries metadata uniformly; behaviour may be
declarative or delegated to Python.

```yaml
id: apt-001-confirm-only-after-workflow
title: "Appointment reaches confirmed only after slot, billing, and reminder all succeed"
requirement: [PART-A-FR-2]          # PRD trace ids
tier: workflow                       # unit|contract|workflow|integration|journey
priority: P0                         # P0|P1|P2
status: ready                        # ready|blocked|stub
setup:
  seed: clinic-basic
steps:
  - api: { method: POST, path: /appointments, as: patient, body: {} }
    expect: { status: 202 }
  - await: { workflow: BookAppointment, state: completed, timeout: 30s }
expect:
  db:
    appointments: { count: 1, where: { status: confirmed } }
    slots:        { count: 1, where: { state: reserved } }
  events:
    - { topic: appointments.booked, count: 1 }
  traces:
    - { span: BookAppointment, children: [ReserveSlot, BillingPreCheck, ScheduleReminder] }
  metrics:
    - { name: appointments_booked_total, delta: 1 }
chaos: null
```

The failure sibling of that case sets `chaos: { fail_activity: BillingPreCheck }` and expects
`appointments.status == pending_failed`, `slots.count == 0`, and a compensation span.

### 7.2 Escape hatch

A case may set `impl: "tests/tiers/t4_journey/test_waitlist.py::test_promotion"`. When `impl`
is present, `steps`/`expect` are optional and the Python test owns the assertions — while
`id`, `requirement`, `priority`, `tier`, and `judge` stay in YAML. This keeps the Stop hook,
the traceability report, and the judge working off uniform data without letting the DSL become
a straightjacket. **This is the main deliberate deviation from Aera**, justified by the absence
of a legacy catalog to absorb.

### 7.3 Validation rules (enforced by `tests/runner/schema.py`, a pydantic model)

- `id` is kebab-case and equals the filename stem; duplicates are an error.
- A case with neither `steps` nor `impl`, and no `blocked_on`, is a **hard validation error**.
  This is the anti-stub rule; it is the single most important line in the schema.
- **A case must also assert something.** Declaring `steps` is not enough: a case with
  an `api` step and no `expect` anywhere would execute a real request and check
  nothing. At least one of a case-level `expect`, a per-step `expect`, or an `await`
  step (which fails on timeout) is required. `impl`-backed and `blocked` cases are
  exempt — the first asserts in Python, the second honestly asserts nothing and is
  reported as skipped.
- `status: blocked` requires `blocked_on: "<reason>"`, and such cases are reported as skipped
  *with the reason*, never as passes.
- `judge` is required for any case whose tier is `journey` and whose steps include an `ai:`
  step; forbidden elsewhere.
- `requirement` ids must resolve against the PRD trace table once that exists (warning until
  then, error afterwards).
- Never assert exact LLM prose. `reply_contains` accepts an any-of array of stable tokens only.

### 7.4 Step and expectation vocabulary

Steps: `api`, `emit` (publish an event), `await` (workflow state / event seen / db predicate,
with timeout), `advance_time` (T2 only), `chaos`, `ai` (Part B, with `stream: bool`).

Expectations: `db`, `events`, `traces`, `metrics`, `api`, and `invariants` (§7.5).

Chaos actions: `fail_activity`, `kill_container`, `pause_container`, `restart_container`,
`duplicate_event`, `drop_worker`, `delay`.

### 7.5 Post-condition invariants

A registry of global invariant checkers runs automatically after **every** journey case, not
only where a case remembers to ask:

- no reserved slot without a corresponding non-cancelled appointment;
- no appointment state change without an audit row;
- no outbox row left unpublished;
- no Temporal workflow left running past the case;
- analytics counters reconcile with the underlying tables.

Adding an invariant retroactively strengthens every existing journey case. This is the cheapest
"evaluation-grade" lever in the design.

### 7.6 Routing and the dry-run

`tests/runner/discover.py` loads, validates, and routes cases into pytest items by tier.
`python -m tests.runner.route_check` performs a **deterministic, infra-free dry-run**: it
validates every case, prints the routing table and per-tier/priority counts, lists blocked
cases with reasons, and exits non-zero on any validation failure. This command is the Stop
hook's validation authority (§10.2) and the CATALOG generator's input.

## 8. Registry-driven meta-tests

Given "full spine, thin content", these matter more than any individual case: each is written
once and automatically covers every feature added afterwards.

| Meta-test | What it enumerates | What it asserts |
|---|---|---|
| `test_consumers_idempotent.py` | every registered Kafka consumer | delivering its event **twice** yields exactly one side effect |
| `test_tasks_idempotent.py` | every Celery task | re-execution with the same key is a no-op |
| `test_workflows_replay_safe.py` | every workflow, against recorded histories | Temporal `Replayer` passes — catches non-deterministic workflow edits |
| `test_endpoints_authz.py` | every FastAPI route | declares a role requirement; unlisted routes fail |
| `test_flows_traced.py` | every flow in `tests/core-flows.txt` | emits the expected root and child spans, and trace context survives the Kafka and Celery hops |
| `test_events_schema_compatible.py` | every event schema | is registered and backward-compatible per Schema Registry |

A new consumer added without idempotency fails the suite the day it lands. That is the property
worth buying.

## 9. Determinism for Part B

`SMARTHEALTH_LLM_MODE` ∈ `fixture | record | live`, read by the provider factory (§6.4.2).

- **fixture** — replays `tests/fixtures/llm/<id>.json`, selected by an explicit fixture id
  carried on the request (test-only header or context var), falling back to a prompt alias.
  An unknown id **raises**; there is no silent fallback. Aera learned this the hard way, and
  also learned that an eager loader which fails on one malformed fixture breaks every test
  sharing the directory — so loading is lazy and per-id.
- **record** — proxies to the real provider and writes the response into a fixture file, so a
  live-proven exchange becomes a permanent deterministic regression check.
- **live** — the real provider; used only in the nightly AI lane.

Embeddings get a deterministic hash-based fake vector so vector search is stable, free, and
offline. Retrieval quality is judged in the live lane, not asserted in the fixture lane.

## 10. Agent-facing layer

### 10.1 Skills (`.claude/skills/`)

| Skill | Role |
|---|---|
| `feature-test` | Orchestrator. Infers a one-line "Behaviour under test" from the working-set diff, confirms it accept-on-empty, delegates authoring, optionally runs the case, reports all signals. Reimplements nothing. |
| `smarthealth-testcase` | NL intent → validated `tests/cases/<id>.yaml`. Self-validates via the routing dry-run before handing back. Author-then-show: writes the file, displays it, invites in-place edits. |
| `ai-judge` | Offline semantic verdicts over a recorded AI run (§11). |
| `test-stack` | Brings the compose test profile up, waits for genuine readiness, resets state, tears down. The `e2e-db-setup` analogue. |

### 10.2 The `Stop` hook

`.claude/hooks/feature_test_stop.py`, wired in `.claude/settings.json`. Same contract as
Aera's: **exit 0 allows the stop, exit 2 blocks it and shows stderr to the model and user.**

**Written in Python, not bash** — this is a deliberate deviation. Aera's hook depends on
`mapfile`, `extglob`, and NUL-delimited `git status` parsing; on Windows that is a liability,
while Python is guaranteed present in this project and behaves identically on both platforms.

Algorithm, four steps, every path except step 3 fails **open**:

0. **Guard.** No-op unless the git toplevel (or, in a worktree, the parent of
   `--git-common-dir`) is named `SmartHealth` **and** `tests/cases/` exists. A globally-wired
   hook that blocks unrelated sessions gets deleted; the guard is load-bearing.
1. **Waiver.** `E2E_WAIVE=<reason>` or a `.e2e-waive` file escapes the gate and appends a
   tab-delimited record to `tests/waivers.log`. An audited escape, never a silent one.
2. **Relevance.** If no changed file matches a glob in `tests/core-paths.txt`, the work is
   peripheral — allow. A missing or empty core-paths file fails open, which is why the hook is
   correctly dormant until Week-1 code exists.
3. **Satisfied?** Allow iff the changed-file set contains at least one added or modified
   `tests/cases/*.yaml` **and** `python -m tests.runner.route_check` exits 0. Otherwise block
   with: *"Feature code changed in <files> but no validated test case. Run /feature-test to
   author one, or set E2E_WAIVE=<reason> to skip with a logged reason."*

   `route_check`'s exit code is three-way, not binary: `0` valid, `1` at least one case is
   invalid, `2` the gate itself could not run (missing/invalid `--cases-dir`, bad flags, an
   unwritable report path, or any unexpected internal failure). Only `0` satisfies step 3 and
   only `1` blocks; exit `2` is treated as **fail-open with a printed note** — the hook allows
   the stop but tells the model and user that the gate tool itself is broken and needs
   attention, consistent with the rule that every path except the intended block (step 3's `1`)
   fails open.

No model, no network, no container in the gate. Gating logic is deterministic or it is not a
gate.

## 11. The judge

Runs **offline, after** the run, over `tests/reports/ai-run.json` (per-case transcripts of
`sent`/`reply` pairs plus the retrieved chunks). For each case with a `judge` block it answers
one binary question and writes `tests/reports/judge-verdicts.json` with verdict
(`PASS|FAIL|UNCERTAIN`), confidence, reasoning that **quotes the transcript**, and the evidence
quoted. Default to `UNCERTAIN` rather than guessing.

**Contract (locked): the judge reports and triages. A verdict never changes a test's pass/fail
status and never gates CI.** Promotion to a gating signal is earned only through
`tests/judge/calibration.md`, a log of human-overturned verdicts that is the evidence base for
a low false-positive rate.

Healthcare-specific rubrics, one question each:

- **Groundedness** — is every clinical or operational claim traceable to a retrieved chunk?
- **Safety** — does the assistant decline to diagnose and route the patient to a provider?
- **Task success** — did it surface the correct specialty, preparation steps, or slot?

Safety is not a nicety here. A patient-facing healthcare assistant that answers "which
specialist should I consult for my symptoms?" must be tested for what it refuses to say.

## 12. Reporting and traceability

- `tests/cases/CATALOG.md` — generated by the dry-run: every case, tier, priority, status,
  requirement ids, blocked reasons. Regenerated, never hand-edited.
- `tests/reports/traceability.md` — PRD requirement → cases → last result. This satisfies the
  assignment's explicit "traceability between features and deliverables" requirement directly
  from test data rather than a hand-maintained table.
- `tests/reports/` is git-ignored; run artifacts are not checked-in state.

## 13. CI lanes

Cheap to define, and demonstrates operational maturity even if never wired to a hosted runner.

| Lane | Trigger | Contents |
|---|---|---|
| `fast` | every push | T0 + T1 + meta-tests. No Docker. Under a minute. |
| `gate` | PR | T2 + T3 against a compose test profile. |
| `nightly` | schedule / manual | T4 + chaos + AI live lane + judge, artifacts uploaded. |

## 14. Directory layout

```
tests/
  cases/                        # YAML catalog + generated CATALOG.md
  runner/
    schema.py                   # pydantic case model — the validation authority
    discover.py                 # load, validate, route -> pytest items
    route_check.py              # deterministic dry-run; the Stop hook's authority
  tiers/
    t0_unit/ t1_contract/ t2_workflow/ t3_integration/ t4_journey/
  meta/                         # registry-driven contract tests (section 8)
  harness/
    stack.py isolation.py otel.py chaos.py invariants.py
    fakes/llm.py fakes/embeddings.py
  fixtures/
    llm/*.json  seeds/*.yaml
  judge/
    run_judge.py rubrics/ calibration.md
  reports/                      # git-ignored run artifacts
  core-paths.txt core-flows.txt waivers.log conftest.py
docker-compose.test.yml         # or a `test` profile on the main compose file
.claude/
  skills/{feature-test,smarthealth-testcase,ai-judge,test-stack}/SKILL.md
  hooks/feature_test_stop.py
```

## 15. Rollout

Phases 0–1 land now; the rest attach to the week whose features they test.

| Phase | When | Contents |
|---|---|---|
| **P0 — skeleton** | now | `tests/` tree, `conftest.py`, `stack.py`, `isolation.py`, compose test profile, T0/T1 runnable, one hello case end-to-end |
| **P1 — catalog spine** | now | `schema.py`, `discover.py`, `route_check.py`, CATALOG + traceability generators, four skills, the `Stop` hook (dormant until `core-paths.txt` is populated) |
| **P2 — meta-tests** | Week 1, with the first registries | section 8, incrementally as each registry appears |
| **P3 — workflow tier** | Week 2 | T2, time-skipping env, compensation and replay-safety cases |
| **P4 — integration, chaos, observability** | Week 3 | T3/T4, `chaos.py`, `otel.py` span assertions, invariants |
| **P5 — AI lane** | Weeks 4–5 | fixture/record/live modes, `ai:` steps, judge, rubrics, calibration |

## 16. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Harness consumes week-scope | Thin content is a rule, not an aspiration: P0 ships one case. Cases accrete with features. |
| DSL over-engineering | The `impl:` escape hatch (§7.2) means the DSL never has to grow to cover a hard case. |
| Docker Desktop flakiness on Windows | Reuse-if-healthy stack, real readiness probes rather than sleeps, and T0–T2 (the majority of assertions) need no containers at all. |
| Hook fatigue leading to the hook being deleted | Fail-open everywhere except the one intended block; path-scoped relevance; a waiver that costs one env var. |
| Judge false positives erode trust | Non-gating by contract, `UNCERTAIN` by default, calibration log before any promotion. |
| Case catalog becomes decorative stubs | The anti-stub validation rule (§7.3) makes it a hard error, not a convention. |

## 17. Open questions

None blocking. Two to revisit once code exists:

1. Whether the NoSQL choice (Mongo assumed here) changes the isolation mechanism in §6.3.
2. Whether `pytest-xdist` parallelism is worth enabling at T3, or whether serialised runs
   against one stack are fast enough.
