# System health: an admin view of monitoring, and an AI health report

Date: 2026-11-17 · Status: built 2026-11-17 · Decision: PRD D-55

## 1. Why

The execution guidelines ask for "AI monitoring metrics" in Week 5. Part A §6 asks for
"monitoring and logging for all critical flows" and the ability to diagnose failures in
booking, availability sync, billing, reminders and background workers. Part B asks the same
for assistant interactions, retrieval, streaming and LLM calls.

The data exists, but only engineers can read it:

- four Grafana dashboards behind the tools login, written in PromQL terms;
- Jaeger, which helps only if you already know what to search for;
- the in-app **AI usage** page (tokens, cost, latency, conversion), which covers usage but
  not health.

An admin inside the app has no single place that answers three questions in plain language:

1. Is the system healthy?
2. What failed recently, and why?
3. How is the AI doing?

This design adds two things:

- **System health**, an in-app admin page.
- **System health snapshot**, a new assistant report kind that writes a plain-English report
  from the same data.

## 2. Scope

**In scope:**
- A Part A module, `app/modules/monitoring/`, that reads Prometheus and Jaeger server-side and
  exposes two admin-only endpoints.
- Three instrumentation additions that fill the gaps in the table in §3.
- A frontend page.
- The `system_health` report kind.
- New Grafana panels for the new metrics.
- Tests, smoke checks and docs.

**Out of scope:**
- Alerting or paging: Grafana's six alert rules stay as they are.
- Log search: logs are JSON on stdout, with no log store.
- Editing thresholds from the UI.
- Business analytics. The existing analytics pages and *Platform analytics* dashboard keep
  them, and this page links there.

## 3. Data sources

Metrics reach Prometheus through the OTel collector (`job` = the service name), plus direct
scrapes of RabbitMQ and kafka-exporter. Traces are in Jaeger.

| Flow / component | Source | Status before this work |
| --- | --- | --- |
| Booking, reschedule, cancel follow-up, billing (post-visit), provider deactivation | `temporal_workflow_completed_total` / `temporal_workflow_failed_total` by `workflow_type`; `temporal_activity_execution_failed_total` | exists |
| API | `http_server_duration_milliseconds_*` by `http_target`, `http_status_code` | exists |
| Events | `smarthealth_outbox_oldest_unpublished_age_seconds`, `smarthealth_outbox_published_total` / `_failed_total`, `smarthealth_consumer_events_total{consumer,outcome}`, `kafka_consumergroup_lag` | exists |
| Queues | `rabbitmq_queue_messages_ready` / `_unacked` | exists |
| Assistant / LLM | `assistant_turns_total{outcome}`, `assistant_first_token_milliseconds`, `assistant_turn_duration_milliseconds`, `assistant_llm_calls_total`, `assistant_llm_tokens_total` | exists |
| Retrieval | none (traces only) | **added:** `knowledge_searches_total{outcome=hit\|no_results\|error}`, `knowledge_search_duration_milliseconds` (histogram), recorded in the Search RPC |
| Notifications, reminders, document extraction | queue depth only | **added:** `smarthealth_celery_tasks_total{task,outcome=success\|retry\|failure}`, recorded from Celery's `task_success` / `task_retry` / `task_failure` signals |
| Liveness of each process | none uniform (an idle process exports nothing) | **added:** `smarthealth.heartbeat` (`smarthealth_heartbeat_seconds` in Prometheus), an observable gauge whose value is the process's Unix time, exported every interval by api, worker, relay, consumers, celery-worker, celery-beat, knowledge, assistant |

A process is **alive** when its heartbeat was seen in the last 60 s. The rule:
`time() - max by (job) (smarthealth_heartbeat_seconds) < 60`. The value is a timestamp, not 1,
because the collector re-serves a dead process's last sample for minutes: a frozen timestamp
ages, a constant would not.

**Traces:** Jaeger's query API, `GET {jaeger}/api/traces?service=<s>&tags={"error":"true"}&start=&end=&limit=`,
for each of `part-a-api`, `part-a-worker`, `knowledge`, `assistant`. The exact service names
are taken from the telemetry setup when this is built. Each trace is reduced to a
`Failure`:

- trace id and start time;
- the service and root operation (for example `POST /appointments`);
- the first span with `error=true`, its operation, and its `exception.message` (or
  `otel.status_description`).

Failures are de-duplicated by trace id and sorted newest first.

**Settings:** `SMARTHEALTH_PROMETHEUS_URL` and `SMARTHEALTH_JAEGER_QUERY_URL`.
- Compose: `http://prometheus:9090` and `http://jaeger:16686`.
- Both charts: the in-cluster services, with Jaeger under its `/jaeger` base path.
- Empty means `not_configured`.

## 4. Part A module `app/modules/monitoring/`

No import of any other `app/modules/*` except `identity` in `router.py`, for `require_role`, through
three declared pairs in the import-boundary meta-test, like every other router.

| File | Purpose |
| --- | --- |
| `client.py` | Async `PrometheusClient.query(promql) -> list[Sample]` (instant query) and `JaegerClient.error_traces(service, start, end, limit)`, on `httpx` with a 3 s timeout. Raises `SourceUnavailable` on any transport or HTTP error |
| `queries.py` | The PromQL strings, one named constant per figure, parameterised by the window (`[24h]` and so on) |
| `rules.py` | Pure functions from numbers to `Status` (`ok \| warn \| bad \| idle \| unknown`) plus a one-line reason; the thresholds table below |
| `traces.py` | Pure: Jaeger trace JSON to `Failure` |
| `service.py` | Runs every query in parallel (`asyncio.gather`), applies the rules, builds the response; 15 s in-process cache (per replica; the contract tier has no Redis) keyed on endpoint and window |
| `schemas.py` | Pydantic response models |
| `router.py` | `GET /monitoring/overview?hours=` and `GET /monitoring/failures?hours=&limit=`; `require_role(admin)` |

`hours` is one of 1, 6, 24 (default 24). `limit` is 1–50 (default 20). No 7-day window:
Prometheus keeps two days here, and each window plus the one before it must fit inside that.
The previous-window figure is `null` when Prometheus has no data that far back.

### 4.1 `GET /monitoring/overview`

```
{
  "window_hours": 24, "generated_at": "...",
  "sources": {"prometheus": "ok|unavailable|not_configured", "jaeger": "..."},
  "components": [ {"name": "API", "job": "smarthealth/part-a-api", "status": "ok", "reason": "heartbeat 8 s ago"} ... ],
  "flows": [ {"key": "booking", "label": "Booking", "total": 42, "failed": 1, "failure_pct": 2.4,
              "p95_seconds": 1.8, "previous_total": 37, "status": "ok", "reason": "..."} ... ],
  "backlogs": [ {"key": "outbox", "label": "Oldest unpublished event", "value": 3,
                 "status": "ok", "reason": "3 s: normal"} ... ],
  "ai": {"turns": 120, "first_token_p95_seconds": 0.9, "error_pct": 0.8,
         "retrieval_no_results_pct": 6.0, "tokens": 81234, "status": "ok", "reason": "..."}
}
```

**Components:** API, Temporal worker, event relay, event consumers, Celery worker, Celery beat,
knowledge, assistant (from heartbeats); RabbitMQ (`up{job="rabbitmq"}`) and Kafka
(`kafka_brokers` from kafka-exporter: the exporter being up says nothing about the brokers). The LLM provider is judged in the AI card, by the assistant's error share.

**Flows:**

| Flow | Source |
| --- | --- |
| booking | workflow type `BookAppointment` |
| reschedule | `Reschedule` |
| cancellation | `CancelFollowUp` |
| billing | `PostVisit` |
| provider sync | `ProviderDeactivation` (consumer outcomes are covered by the dead-letter backlog) |
| notifications and reminders | Celery `notifications.send`; a reminder is a notification the visit timer queued, so the two share one task and one flow |
| document extraction | Celery `clinical.extract_document` |
| retrieval | knowledge searches |
| assistant chat | assistant turns |

`p95_seconds` comes from the matching duration histogram where one exists, else `null`.
`previous_total` is the same figure for the window before, for the trend arrow. Temporal labels
`workflow_type` with the **registered** name (`@workflow.defn(name=...)`), not the class name;
a meta-test ties every workflow and task flow to a registered name.

**Backlogs:**
- outbox age;
- the largest consumer lag (the worst group);
- the largest RabbitMQ ready count (the worst queue, dead-letter queues excluded);
- events dead-lettered in the window (consumer outcome `dead_lettered`);
- notifications waiting in the dead-letter queue now (its depth).

Each backlog is the worst case, not a breakdown: the page answers "is anything behind?", and
Grafana's *Async backbone* dashboard has the per-queue and per-group detail. The value's unit is
in the reason ("240 messages: falling behind").

`sources.jaeger` comes from a cheap probe (`GET /api/services`) run with the queries.

Each card carries its own status. If Prometheus is unreachable, every Prometheus-backed card
says `unknown` / "metrics unavailable", `sources.prometheus` says `unavailable`, and the
endpoint still answers 200.

### 4.2 `GET /monitoring/failures`

```
{ "window_hours": 24, "source": "ok|unavailable|not_configured",
  "failures": [ {"trace_id": "...", "at": "...", "service": "part-a-worker",
                 "operation": "RunWorkflow:BookAppointment",
                 "failed_step": "RunActivity:billing.precheck", "message": "card declined",
                 "trace_path": "/jaeger/trace/<id>"} ... ] }
```

`trace_path` is relative. The browser opens it under the same host (the tools login applies).

### 4.3 Thresholds (`rules.py`, quoted by the docs)

| Figure | warn | bad |
| --- | --- | --- |
| Heartbeat age | — | > 60 s (process down) |
| Flow failure share | > 5 % | > 20 % |
| Flow with zero traffic | `idle`, never `bad` | |
| Outbox oldest unpublished | > 60 s | > 300 s |
| Consumer lag (any group) | > 100 | > 1000 |
| RabbitMQ ready (any queue) | > 100 | > 1000 |
| Dead letters (in window, or waiting) | ≥ 1 | ≥ 10 |
| Assistant error share | > 5 % | > 20 % (the flow failure-share rule) |
| Time to first word p95 | > 5 s | > 15 s |
| Retrieval no-results share | > 25 % | > 50 % |

A failure share needs at least 5 events before it can be `warn` or `bad`, so one failure in
two bookings is not "bad".

Every threshold is *at or above*: a value equal to `warn` is `warn`.

## 5. Frontend: `frontend/src/pages/Health.tsx`

- An admin-only nav item, **System health**, beside AI usage.
- Range buttons: 1 hour / 6 hours / 24 hours.
- Auto-refresh every 30 s.
- Sections, in this order:
  1. **Status strip:** one tile per component, colour plus reason.
  2. **Critical flows:** a table with total, failed, failure %, p95, a trend arrow and status.
  3. **Recent failures:** time, flow/service, failed step, message and a "View trace" link.
  4. **Backlogs:** cards with a plain label ("normal", "falling behind").
  5. **AI health:** cards plus a link to AI usage.
  6. A **Write a health report** button that starts a `system_health` report and navigates
     to Reports.

A banner names any source that is `unavailable` / `not_configured`.

## 6. Assistant report kind `system_health`

In `services/assistant/assistant_service/reports.py`:
- `KINDS["system_health"] = ("System health snapshot", 1)`, admin only. A non-admin
  `StartReport` for it is refused with `permission_denied`.
- `_write` calls `GET /monitoring/overview?hours=24` and `GET /monitoring/failures?hours=24`
  **with the caller's token**, emits both as tool cards, then the narrative.

**The narrative:**
- The model's instructions: summarise what is healthy, what is not, the likely cause from the
  failed steps, and what to look at next, using only the given figures.
- The prompt carries only the JSON above: no patient data. Failure messages are exception
  text, and the PRD records that this text is sent to the LLM provider.
- **Offline / no model:** a deterministic template writes one overall sentence (healthy, N
  need attention, or can't tell), the "Needs attention" items (bad first) and the most recent
  failure, from the statuses and reasons.
- **Never a false all-clear:** when a source is unreachable or items are `unknown`, the report
  says it can't tell and names the source; it never calls an unmeasured system healthy.
- Every number shown comes from the cards. The model phrases and never computes, the same
  rule as the existing report kinds.

## 7. Grafana

- *Async backbone* gains Celery task outcomes.
- *Assistant* gains retrieval searches by outcome and search p95.
- *Service overview* gains a timeseries of seconds since each job was last seen (`time() - max by (job) (smarthealth_heartbeat_seconds)`), so a gap shows when a process died.
- `scripts/sync_chart_files.py` copies the dashboards into the charts, as today.

## 8. Testing

| Tier | What it proves |
| --- | --- |
| T0 unit | `rules.py`: every threshold boundary, the 5-event minimum, `idle`; `traces.py`: recorded Jaeger JSON (success, nested error, no message) to `Failure` |
| T1 contract | Both endpoints with fake Prometheus/Jaeger (`respx` or a fake client): happy path; Prometheus down (200, cards `unknown`); Jaeger down; not configured; 403 for patient, provider and front desk; cache hit within 15 s |
| T1 contract (assistant) | `system_health` against `tests/harness/fake_part_a.py` extended with the two endpoints: scripted model and offline template; non-admin refused |
| T1 / meta | The new metrics: knowledge Search increments `knowledge_searches_total` by outcome; Celery signal handlers increment by outcome; `register_heartbeat` exports one series per provider (tested directly: the suite installs its own telemetry first, so `setup_telemetry` returns before registering); every Part A entry point calls `setup_telemetry`, which calls it |
| Frontend (Vitest) | `Health.tsx` renders all sections from a fixture; the unavailable banner; non-admin cannot reach it |
| T3 docker | Against the test stack's real Prometheus and Jaeger: every PromQL query in `queries.py` is accepted (no `SourceUnavailable`); an error span exported over OTLP under a unique service name comes back from `JaegerClient.error_traces` and reduces to a `Failure` with its message |
| Smoke | `k3d_smoke.py` and `aws_check.sh` call `/monitoring/overview` as admin and require `sources.prometheus == "ok"` and every component `ok` |

New core-path code under `app/modules/**` gets a validated case in `tests/cases/`
(smarthealth-testcase).

## 9. Docs

- **PRD:** D-55 (this design, with the rejected alternatives: a separate ops service, a
  browser querying Prometheus/Jaeger directly, embedding Grafana) and requirement traces to
  Week 5 "AI monitoring metrics", Part A §6 and Part B Observability.
- **ARCHITECTURE:** a monitoring module section.
- **README:** the endpoint/role table and where the page is.
- **Site:** the feature, the flow, the tools/alternatives row.
- `gen_docs_data.py` regenerates the OpenAPI data.

## 10. Risks

- **Jaeger's `/api/traces` is its UI's internal API**, not a stable public contract. It is
  pinned to the deployed Jaeger image (1.62) and covered by the T3 test.
- **Jaeger all-in-one keeps traces in memory.** After a restart the failure list starts
  empty, so a quiet list right after a restart is not proof that nothing failed (PRD R-37).
- **Exception text goes to the LLM provider** in the AI report. It carries no clinical field
  names by convention, but it is free text; recorded in the PRD.
