"""Every PromQL question the health page asks (system health spec §3, §4.1).

Names are as Prometheus sees them after the OTel collector: dots become underscores, a unit
suffix is added (`_milliseconds`, `_seconds`), counters end `_total`, and every series has
`job="smarthealth/<service.name>"`. The T3 test sends every query here to the real
Prometheus, so a typo fails there, not on the page.
"""

from __future__ import annotations

from dataclasses import dataclass

#: (job, what the page calls it). The heartbeat's `job` label is the process's service.name.
COMPONENTS: list[tuple[str, str]] = [
    ("smarthealth/part-a-api", "API"),
    ("smarthealth/part-a-worker", "Workflow worker"),
    ("smarthealth/part-a-relay", "Event relay"),
    ("smarthealth/part-a-consumers", "Event consumers"),
    ("smarthealth/part-a-celery", "Background jobs"),
    ("smarthealth/part-a-celery-beat", "Job scheduler"),
    ("smarthealth/knowledge", "Knowledge search"),
    ("smarthealth/assistant", "Assistant"),
]

HEARTBEAT_AGE = "time() - max by (job) (smarthealth_heartbeat_seconds)"
BROKER_UP = {
    "rabbitmq": 'max(up{job="rabbitmq"})',
    "kafka": "max(kafka_brokers)",
}


@dataclass(frozen=True)
class Flow:
    key: str
    label: str
    #: "workflow" (Temporal workflow_type), "task" (Celery task name), "search", "turn"
    kind: str
    name: str = ""


#: A workflow flow's `name` is the registered type (`@workflow.defn(name=...)`), which is what
#: Temporal puts in `workflow_type`; tests/meta/test_monitoring_flows.py checks it. A
#: workflow total counts completed + failed runs only: timed-out, cancelled and terminated
#: runs are in neither.
FLOWS: list[Flow] = [
    Flow("booking", "Booking", "workflow", "BookAppointment"),
    Flow("reschedule", "Reschedule", "workflow", "Reschedule"),
    Flow("cancellation", "Cancellation follow-up", "workflow", "CancelFollowUp"),
    Flow("billing", "Billing after a visit", "workflow", "PostVisit"),
    Flow("provider_sync", "Provider deactivation", "workflow", "ProviderDeactivation"),
    Flow("notifications", "Notifications and reminders", "task", "notifications.send"),
    Flow("documents", "Document text extraction", "task", "clinical.extract_document"),
    Flow("retrieval", "Knowledge search", "search"),
    Flow("assistant", "Assistant answers", "turn"),
]


def _sum(expr: str) -> str:
    return f"sum({expr}) or vector(0)"


def flow_total(flow: Flow, w: str, offset: str = "") -> str:
    """The runs in the window. With an `offset`, the same window that long ago, with no
    `or vector(0)`: an unanswered query means "no comparison", not "zero runs"."""
    off = f" offset {offset}" if offset else ""
    wrap = (lambda expr: f"sum({expr})") if offset else _sum
    if flow.kind == "workflow" and offset:
        # One selector over both counters: Temporal creates the `failed` series only at a
        # type's first failure, and `a + <nothing>` is nothing -- a never-failed workflow
        # would otherwise never get a comparison.
        return wrap(f'increase({{__name__=~"temporal_workflow_(completed|failed)_total",'
                    f'workflow_type="{flow.name}"}}[{w}]{off})')
    if flow.kind == "workflow":
        sel = f'{{workflow_type="{flow.name}"}}'
        return (f"({wrap(f'increase(temporal_workflow_completed_total{sel}[{w}]{off})')})"
                f" + ({wrap(f'increase(temporal_workflow_failed_total{sel}[{w}]{off})')})")
    if flow.kind == "task":
        return wrap(f'increase(smarthealth_celery_tasks_total{{task="{flow.name}",'
                    f'outcome=~"success|failure"}}[{w}]{off})')
    if flow.kind == "search":
        return wrap(f"increase(knowledge_searches_total[{w}]{off})")
    return wrap(f"increase(assistant_turns_total[{w}]{off})")


def flow_failed(flow: Flow, w: str) -> str:
    if flow.kind == "workflow":
        return _sum(f'increase(temporal_workflow_failed_total{{workflow_type="{flow.name}"}}[{w}])')
    if flow.kind == "task":
        return _sum(f'increase(smarthealth_celery_tasks_total{{task="{flow.name}",'
                    f'outcome="failure"}}[{w}])')
    if flow.kind == "search":
        return _sum(f'increase(knowledge_searches_total{{outcome="error"}}[{w}])')
    return _sum(f'increase(assistant_turns_total{{outcome="error"}}[{w}])')


def flow_p95_ms(flow: Flow, w: str) -> str | None:
    """p95 duration in ms where a histogram exists; Celery tasks have none."""
    prefix = "histogram_quantile(0.95, sum by (le) (increase("
    if flow.kind == "workflow":
        metric = ("temporal_workflow_endtoend_latency_milliseconds_bucket"
                  f'{{workflow_type="{flow.name}"}}')
    elif flow.kind == "search":
        metric = "knowledge_search_duration_milliseconds_bucket"
    elif flow.kind == "turn":
        metric = "assistant_turn_duration_milliseconds_bucket"
    else:
        return None
    return f"{prefix}{metric}[{w}])))"


def backlogs(w: str) -> list[tuple[str, str, str, str]]:
    """(key, label, rule key, PromQL)."""
    return [
        ("outbox", "Oldest unpublished event", "outbox_age_s",
         "max(smarthealth_outbox_oldest_unpublished_age_seconds)"),
        ("consumer_lag", "Largest event consumer lag", "consumer_lag",
         "max(sum by (consumergroup) (kafka_consumergroup_lag))"),
        ("queue_ready", "Largest job queue", "queue_ready",
         r'max(rabbitmq_queue_messages_ready{queue!~".*\\.dlq"})'),
        ("event_dead_letters", "Events dead-lettered", "dead_letters",
         _sum(f'increase(smarthealth_consumer_events_total{{outcome="dead_lettered"}}[{w}])')),
        ("job_dead_letters", "Notifications dead-lettered", "dead_letters",
         _sum(r'rabbitmq_queue_messages_ready{queue=~".*\\.dlq"}')),
    ]


def ai(w: str) -> dict[str, str]:
    return {
        "turns": _sum(f"increase(assistant_turns_total[{w}])"),
        "errors": _sum(f'increase(assistant_turns_total{{outcome="error"}}[{w}])'),
        "first_token_p95_ms": ("histogram_quantile(0.95, sum by (le) "
                               f"(increase(assistant_first_token_milliseconds_bucket[{w}])))"),
        "searches": _sum(f"increase(knowledge_searches_total[{w}])"),
        "no_results": _sum(f'increase(knowledge_searches_total{{outcome="no_results"}}[{w}])'),
        "tokens": _sum(f"increase(assistant_llm_tokens_total[{w}])"),
    }


def every_query(w: str = "24h") -> list[str]:
    """All of the above, for the T3 test that sends each to a real Prometheus."""
    out = [HEARTBEAT_AGE, *BROKER_UP.values()]
    for flow in FLOWS:
        out += [flow_total(flow, w), flow_total(flow, w, w), flow_failed(flow, w)]
        if p95 := flow_p95_ms(flow, w):
            out.append(p95)
    out += [q for *_, q in backlogs(w)]
    out += list(ai(w).values())
    return out
