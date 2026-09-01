---
name: smarthealth-testcase
description: "Turn a natural-language test description (e.g. 'test that booking stays unconfirmed when the billing pre-check fails') into a validated, executable case YAML at tests/cases/<id>.yaml — tier, priority, requirement trace, steps, expectations, and a judge prompt for AI cases. Self-validates via the routing dry-run before handing back."
---

# smarthealth-testcase — natural language to a validated case

Turns a plain-English description into a file `tests/runner/discover.py` can route and pytest can execute. Companion skills: **test-stack** (boots the infrastructure a case may need) and **ai-judge** (evaluates the `judge` prompt this skill authors).

## 1. Choose the tier — this is the most consequential decision

| Tier | Choose it when the property is about… | Infrastructure |
|---|---|---|
| `unit` | pure logic: a state transition, a validator, a policy | none |
| `contract` | the HTTP surface: status codes, payload shape, role enforcement | none |
| `workflow` | a Temporal workflow: ordering, compensation, timers, replay-safety | SDK time-skipping |
| `integration` | real infrastructure: a consumer, a repository, an event round-trip, idempotency under duplicate delivery | compose test stack |
| `journey` | a multi-step business flow end to end, or deliberate failure injection | full stack |

Pick the **cheapest tier that can actually prove the property.** A booking invariant belongs in `workflow`, not `journey` — it runs in about a second and proves the same thing. Only reach for `journey` when the point is the whole stack behaving together.

## 2. Write `tests/cases/<id>.yaml`

| Field | Rule |
|---|---|
| `id` | kebab-case, and **must equal the filename stem**. Prefix by area: `apt-` appointments, `vst-` visits, `pat-` patients, `prv-` providers, `evt-` events, `ai-` assistant, `sys-` platform. |
| `title` | One line stating the property under test, not the mechanics. |
| `requirement` | PRD trace ids, e.g. `[PART-A-FR-2]`. Always fill this in — the traceability report is a graded deliverable, and a case with no requirement shows up as a gap under `(none)`. |
| `tier` | Per §1. |
| `priority` | `P0` if it guards a core flow (booking, cancellation, visit lifecycle, idempotency, a security boundary); `P1` otherwise. |
| `status` | `ready`, or `blocked` with a `blocked_on` reason. |
| `steps` | A list, each with exactly one kind: `api`, `emit`, `await`, `advance_time`, `chaos`, `ai`. |
| `expect` | Post-journey assertions: `db`, `events`, `traces`, `metrics`, `api`, `invariants`. |
| `judge` | **Mandatory** for any case with an `ai` step; forbidden otherwise. See §4. |
| `impl` | Escape hatch — `path/to/test.py::test_name`. Use when the assertion genuinely cannot be expressed declaratively. |

Example:

```yaml
id: apt-001-billing-failure-leaves-no-orphan-slot
title: "A failed billing pre-check leaves the appointment unconfirmed and the slot free"
requirement: [PART-A-FR-2]
tier: workflow
priority: P0
status: ready
setup:
  seed: clinic-basic
steps:
  - api: { method: POST, path: /appointments, as: patient, body: { provider_id: p1, slot: s1 } }
    expect: { status: 202 }
  - chaos: { fail_activity: BillingPreCheck }
  - await: { workflow: BookAppointment, state: completed, timeout: 30s }
expect:
  db:
    appointments: { count: 1, where: { status: failed } }
    slots: { count: 0, where: { state: reserved } }
  invariants: [no_orphan_slots]
```

## 3. Hard rules

- **Never author a case that asserts nothing.** Two separate schema rules enforce this:
  1. A case must declare `steps` or `impl`, or be `status: blocked` with a reason.
  2. A case must **assert** something — a case-level `expect`, a per-step `expect`, or an `await` step. Declaring `steps` is not enough: an `api` step with no `expect` fires a real request and checks nothing, so a 500 would pass.
  If it genuinely cannot be automated yet, set `status: blocked` with a `blocked_on` reason — honestly skipped, never a false pass.
- **Prefer the cheapest tier.** Reaching for `journey` when `workflow` would do costs a minute per run, forever.
- **Never assert exact LLM prose.** For `ai` steps, assert stable tokens only, and put the semantic property in the `judge` prompt.
- **A step kind with no engine handler will FAIL loudly.** Today the engine implements only `api` steps and `api` expectations. That failure is deliberate — either implement the handler or mark the case blocked, never leave it looking automated.

## 4. Judge prompts (AI cases only)

Read later by **ai-judge**, offline, against the recorded transcript alone. Write it so it can be answered from the transcript with no other context:

- **Binary-decidable.** A yes/no question with one correct answer. Not "how did it do?".
- **About intent, not wording.** "Did it decline to diagnose and route to a provider?", not "did the reply contain the word 'provider'" — that is a structural check's job.
- **About what assertions cannot reach**: groundedness, safety, correctness, coherence.
- **One question per case.** Two independent properties means two cases.

## 5. Self-validate — required before handing back

```bash
python -m tests.runner.route_check
```

Exit 0 means every case is valid; 1 means at least one is invalid; 2 means the check could not run at all. Confirm the new id appears in the table and routes to the intended bucket (`declarative` or `impl`, not `unsupported`). Then run it:

```bash
python -m pytest tests/tiers/test_catalog.py -k <id> -v
```

If it routes to `unsupported`, the case uses a step or expectation kind with no engine handler yet — either implement the handler, or set `status: blocked` with a reason.

If you changed the catalog, regenerate the committed index:

```bash
python -m tests.runner.route_check --write-catalog
```

A test fails if `tests/cases/CATALOG.md` drifts from the cases it describes.

## 6. Author-then-show

Write the validated file, then display its full contents and path, and invite the developer to edit it in place. Do not present a preview for approval first — the file already validates, so editing is lower-friction than re-drafting.
