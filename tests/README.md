# SmartHealth Test Harness

Design: [`docs/superpowers/specs/2026-09-01-testing-harness-design.md`](../docs/superpowers/specs/2026-09-01-testing-harness-design.md)

## Running

All commands assume the project virtualenv is active (`source .venv/Scripts/activate` on
Windows in Git Bash, `source .venv/bin/activate` on POSIX). Without it the ambient
interpreter may lack pytest and PyYAML.

```bash
python -m pytest -m "not docker"          # fast lane: T0, T1, meta — no containers
python -m pytest -m docker                # T3/T4 — needs the compose test stack
python -m pytest -m workflow              # one tier
python -m pytest tests/tiers/test_catalog.py -k <case-id>   # one case
python -m tests.runner.route_check        # validate and route the catalog (no infra)
python -m tests.runner.route_check --write-catalog --write-traceability
```

## Tiers

| Marker | Tier | Infrastructure | Proves |
|---|---|---|---|
| `unit` | T0 | none | domain logic, state transitions, validators |
| `contract` | T1 | none (ASGI transport) | API surface, authz, schema compatibility |
| `workflow` | T2 | Temporal SDK time-skipping | workflow correctness, compensation, timers |
| `integration` | T3 | compose test stack | schema constraints, repositories, consumers, tasks, idempotency |
| `journey` | T4 | full stack | business journeys, chaos, traces, invariants |

Choose the cheapest tier that can prove the property. Most reliability invariants belong
in `workflow`, which runs in about a second against the SDK's time-skipping environment —
not in `journey`.

## The case catalog

Cases are YAML under `tests/cases/`, validated by `tests/runner/schema.py` — the single
authority. `tests/tiers/test_catalog.py` collects them into pytest items.

Author one with the **smarthealth-testcase** skill rather than by hand: it fills the
metadata, self-validates, and picks the tier.

Two rules keep the catalog honest, and both are hard validation errors:

1. A case must declare `steps` or `impl`, or be `status: blocked` with a `blocked_on` reason.
2. **A case must assert something** — a case-level `expect`, a per-step `expect`, or an
   `await` step. Declaring `steps` is not enough: an `api` step with no `expect` fires a
   real request and checks nothing, so a 500 response would pass. The expectation must
   also declare a real check, not merely be present — `expect: {}` and `expect: { api: {} }`
   validate as a shape but assert nothing, and are rejected the same as a missing `expect`.

Also:

- `id` must equal the filename stem.
- `requirement` should always be filled in — `tests/reports/traceability.md` is generated
  from it and is a graded deliverable. Cases without one collect under `(none)`.
- A step or expectation kind with no engine handler fails loudly. Implement it or mark the
  case blocked — never leave it looking automated.
- `tests/cases/CATALOG.md` is generated. A test fails if it drifts from the cases it
  describes; regenerate with `python -m tests.runner.route_check --write-catalog`.

### Engine coverage today

The engine implements the `api` step kind and the `api` expectation kind. `emit`, `await`,
`advance_time`, `chaos`, `ai` and the `db`/`events`/`traces`/`metrics`/`invariants`
expectations are authorable now and raise `NotImplementedError` when run, until their phase
lands.

Two sharp edges worth knowing:

- A **list-valued** expectation is an exact-equality check, not a subset check. Expecting
  `{"items": [{"id": 1}]}` fails against `[{"id": 1}, {"id": 2}]`. It fails loudly and
  clearly, but it is not "contains".
- An **empty expectation** — `expect: {}`, `expect: { api: {} }`, a per-step `expect: {}`,
  or `json_contains: {}` — is rejected by the schema. An expectation that declares no
  check looks like an assertion and is not one.

## Isolation

Every run gets its own database, topic prefix, consumer group, vhost, and task queue via
`tests/harness/isolation.py`, so a shared stack behaves like a private one. Ask the
`isolation` fixture for names; never hardcode them, and never add teardown that truncates
shared tables.

## The `Stop` hook

`.claude/hooks/feature_test_stop.py` blocks a session from stopping when a file matching a
glob in `tests/core-paths.txt` changed without a validated case. It fails open everywhere
else — no globs configured, no core path touched, missing dependencies, `route_check`
unable to run, or any unexpected error all allow the stop.

Escape hatch: `E2E_WAIVE="<reason>"`, logged to `tests/waivers.log`.

`route_check`'s exit codes are the gate's contract: **0** every case valid, **1** at least
one invalid, **2** the check could not run at all. The hook treats 2 as fail-open — a
broken gate must not block.

## Known constraints — read before extending the harness

Verified during the build. Each is inert today and bites the first task that ignores it.

**1. Never call `get_settings()` from a session-scoped fixture.** It is an `lru_cache`
singleton. The autouse `_reset_settings_cache` fixture clears the *cache* between tests but
cannot invalidate a `Settings` object a session-scoped fixture already captured — that
consumer would silently serve the pre-session value while every `monkeypatch.setenv`
appears to do nothing. Read `os.environ` directly in session-scoped fixtures.

**2. `httpx.ASGITransport` does not run FastAPI's lifespan.** The transport only sends an
`"http"` scope, so `api_client` exercises an app whose startup handlers never ran. Harmless
while `/health` depends on nothing. Once a DB pool, Kafka producer, or Temporal client is
wired through a lifespan handler, any endpoint reached via `api_client` that reads
`app.state.<resource>` fails on uninitialised state. Drive the lifespan explicitly then, or
keep those endpoints at T3 against the real stack.

**3. `/health`'s `-> dict[str, str]` annotation is an enforced response model.** FastAPI
validates against it: returning a boolean or a nested object raises
`ResponseValidationError`. Growing the payload beyond flat strings needs a deliberate
annotation change.

**4. Redis and RabbitMQ have no persistent volume.** A `down`/`up` cycle discards their
state. Postgres, Mongo, and Kafka persist.

**5. Integration tests get a per-run database, created and dropped per session.**
`tests/tiers/t3_integration/conftest.py` creates `<prefix>db`, runs `alembic upgrade
head`, and drops it at session end. It builds `Settings(...)` directly rather than
calling `get_settings()` — see constraint 1. A crashed session can leave the database
behind; list strays with `docker compose -p smarthealth-test --env-file .env.test -f
docker-compose.infra.yml exec -T postgres psql -U smarthealth -c "\l"`.

**6. The integration fixture runs each test in one transaction, rolled back.** Anything
depending on `now()` therefore sees a single frozen timestamp for the whole test — which
is why the `updated_at` trigger uses `clock_timestamp()`.

## Phase status

| Phase | Status |
|---|---|
| P0 skeleton, P1 catalog spine + agent layer | done |
| P2 registry-driven meta-tests | with Week 1 |
| P3 workflow tier | Week 2 |
| P4 integration, chaos, observability | Week 3 |
| P5 AI lane, judge | Weeks 4–5 |
