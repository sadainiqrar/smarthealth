---
name: test-stack
description: "Bring the SmartHealth compose test stack up, check that every service reports healthy, reset it, or tear it down. Use before running any integration (T3) or journey (T4) test, and when a test fails with a connection error."
---

# test-stack — the infrastructure the integration and journey tiers need

One topology (`docker-compose.infra.yml`) runs as two stacks under different compose project names. The test stack uses `.env.test`, which offsets every port so a dev stack and a test stack coexist.

## Commands

**Bring it up** (blocks until every healthcheck passes — never a sleep):

```bash
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml up -d --wait
```

First boot pulls images and takes a few minutes. Later boots are seconds. `up --wait` is idempotent and self-healing: run it against a partially-stopped stack and it recovers the missing service and re-verifies health before returning.

**Check health:**

```bash
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml ps
```

Or from Python: `TestStack().status()` / `.unhealthy()` / `.is_running()` in `tests/harness/stack.py`.

**Logs for one service** (the first thing to read when a service will not go healthy):

```bash
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml logs kafka
```

**Tear down** (add `--volumes` to discard data):

```bash
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml down --remove-orphans
```

## Running tests against it

```bash
python -m pytest -m docker          # only the tests that need the stack
python -m pytest -m "not docker"    # everything that does not (the default fast lane)
```

The session `stack` fixture reuses an already-running healthy stack. Override with `SMARTHEALTH_TEST_STACK`: `reuse` (default), `fresh` (always boot), `external` (assume someone else started it).

## Isolation — why a shared stack is safe

Every run gets its own database, topic prefix, consumer group, vhost, and task queue via `tests/harness/isolation.py`. Two runs against one stack never observe each other — this is verified, not assumed. Do not add per-test teardown that truncates shared tables; that breaks parallel runs. Ask the `isolation` fixture for names instead of hardcoding them.

Note: Redis and RabbitMQ have no persistent volume, so a `down`/`up` cycle discards their state. Postgres, Mongo, and Kafka do persist.

## Ports

Postgres 15432 · Mongo 27018 · Redis 16379 · RabbitMQ 5673 (management 15673) · Kafka 19092 · Schema Registry 18081 · Temporal 17233.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `port is already allocated` | A dev stack is on the same port. The test stack uses offsets from `.env.test`; check nothing else claimed them. |
| Kafka never goes healthy | Usually a stale volume from a changed `CLUSTER_ID`. `down --volumes`, then up. |
| `temporal` unhealthy, `postgres` healthy | Temporal runs a schema setup on first boot and takes ~60s. Check its logs before assuming failure. |
| Tests hang on connect | The stack is not running. Bring it up; do not raise the test's timeout. |
| `up` hangs for minutes then fails | A service is genuinely broken. `up --wait` waits up to 300s before raising. Read that service's logs. |
