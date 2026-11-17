"""Every PromQL question the health page asks (system health spec §3, §4.1).

Names are as Prometheus sees them after the OTel collector: dots become underscores, a unit
suffix is added (`_milliseconds`, `_seconds`), counters end `_total`, and every series has
`job="smarthealth/<service.name>"`. The T3 test sends every query here to the real
Prometheus and proves each is valid PromQL, so a *syntax* typo fails there. A wrong metric
name is still valid: it returns empty and shows on the page as "no data" or idle; the k3d
and AWS smoke runs catch that (every component must be ok). tests/meta/test_monitoring_flows.py
checks the label values -- each workflow type and Celery task name is a registered one.
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
    #: "booking" (`smarthealth_bookings_total` by outcome, p95 from the BookAppointment
    #: workflow), "workflow" (Temporal workflow_type), "task" (Celery task name), "search",
    #: "turn"
    kind: str
    name: str = ""


#: A workflow flow's `name` is the registered type (`@workflow.defn(name=...)`), which is what
#: Temporal puts in `workflow_type`; tests/meta/test_monitoring_flows.py checks it. A
#: workflow total counts completed + failed runs only: timed-out, cancelled and terminated
#: runs are in neither. Booking is not counted by its workflow: a booking that compensates
#: (a declined payment, a lost slot) ends `failed` while `BookAppointmentWorkflow` itself
#: completes normally, so the booking activities count outcomes instead (`booking.confirm`,
#: `booking.fail_booking`, and the stale-pending reconciler).
FLOWS: list[Flow] = [
    Flow("booking", "Booking", "booking", "BookAppointment"),
    Flow("reschedule", "Reschedule", "workflow", "Reschedule"),
    Flow("cancellation", "Cancellation follow-up", "workflow", "CancelFollowUp"),
    Flow("billing", "Billing after a visit", "workflow", "PostVisit"),
    Flow("provider_sync", "Provider deactivation", "workflow", "ProviderDeactivation"),
    Flow("notifications", "Notifications and reminders", "task", "notifications.send"),
    Flow("documents", "Document text extraction", "task", "clinical.extract_document"),
    Flow("retrieval", "Knowledge search", "search"),
    Flow("assistant", "Assistant answers", "turn"),
]

_ZERO = "vector(0)"


def _hours(window: str) -> int:
    if not window.endswith("h") or not window[:-1].isdigit():
        raise ValueError(f"windows are whole hours like '24h', not {window!r}")
    return int(window[:-1])


def runs(selector: str, w: str, offset: str = "") -> str:
    """How much the counters `selector` (one metric name) grew in the window of length
    `w` ending `offset` ago. Empty when no series has a sample in the window.

    `increase` never counts a series' first sample: a series born inside the window (a
    process's first export, or any restart that gives the series a new `instance`) has its
    first value -- often every run so far -- dropped. So each series that did not exist at
    the window's start adds its first value too: `min_over_time` of a counter over the
    window is that value, and `unless <selector> offset <start>` keeps only series absent at
    the start (matching ignores `__name__`, which `min_over_time` drops). A series with one
    sample in the window has no `increase` at all; `or min_over_time(...) * 0` keeps it as
    zero so that the window still answers.
    """
    off = f" offset {offset}" if offset else ""
    start = f"{_hours(w) + (_hours(offset) if offset else 0)}h"
    rng = f"{selector}[{w}]{off}"
    return (f"(sum(increase({rng}) or min_over_time({rng}) * 0)"
            f" + (sum(min_over_time({rng}) unless {selector} offset {start}) or {_ZERO}))")


def _current(selector: str, w: str) -> str:
    """A current-window figure: no series at all means zero runs."""
    return f"{runs(selector, w)} or {_ZERO}"


def _outcomes(flow: Flow) -> tuple[str, str]:
    """(a selector for every counted run, a selector for the failed runs) of a non-workflow
    flow."""
    if flow.kind == "booking":
        return ('smarthealth_bookings_total{outcome=~"confirmed|failed"}',
                'smarthealth_bookings_total{outcome="failed"}')
    if flow.kind == "task":
        task = f'task="{flow.name}"'
        return (f'smarthealth_celery_tasks_total{{{task},outcome=~"success|failure"}}',
                f'smarthealth_celery_tasks_total{{{task},outcome="failure"}}')
    if flow.kind == "search":
        return "knowledge_searches_total", 'knowledge_searches_total{outcome="error"}'
    return "assistant_turns_total", 'assistant_turns_total{outcome="error"}'


def flow_total(flow: Flow, w: str, offset: str = "") -> str:
    """The runs in the window. With an `offset`, the same window that long ago, with no
    `or vector(0)` around it: an unanswered query means "no comparison", not "zero runs"."""
    if flow.kind == "workflow":
        # Two metrics, added: one selector over both names would give min_over_time two
        # series with the same labels once `__name__` is dropped. Temporal creates the
        # `failed` series only at a type's first failure, and `a + <nothing>` is nothing,
        # so `failed` always falls back to zero -- a never-failed workflow still compares.
        sel = f'{{workflow_type="{flow.name}"}}'
        completed = runs(f"temporal_workflow_completed_total{sel}", w, offset)
        failed = runs(f"temporal_workflow_failed_total{sel}", w, offset)
        if offset:
            return f"{completed} + ({failed} or {_ZERO})"
        return f"({completed} or {_ZERO}) + ({failed} or {_ZERO})"
    every, _ = _outcomes(flow)
    return runs(every, w, offset) if offset else _current(every, w)


def flow_failed(flow: Flow, w: str) -> str:
    if flow.kind == "workflow":
        return _current(f'temporal_workflow_failed_total{{workflow_type="{flow.name}"}}', w)
    _, failed = _outcomes(flow)
    return _current(failed, w)


def flow_p95_ms(flow: Flow, w: str) -> str | None:
    """p95 duration in ms where a histogram exists; Celery tasks have none."""
    prefix = "histogram_quantile(0.95, sum by (le) (increase("
    if flow.kind in ("workflow", "booking"):
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
         _current('smarthealth_consumer_events_total{outcome="dead_lettered"}', w)),
        ("job_dead_letters", "Notifications dead-lettered", "dead_letters",
         r'sum(rabbitmq_queue_messages_ready{queue=~".*\\.dlq"}) or vector(0)'),
    ]


def ai(w: str) -> dict[str, str]:
    return {
        "turns": _current("assistant_turns_total", w),
        "errors": _current('assistant_turns_total{outcome="error"}', w),
        "first_token_p95_ms": ("histogram_quantile(0.95, sum by (le) "
                               f"(increase(assistant_first_token_milliseconds_bucket[{w}])))"),
        "searches": _current("knowledge_searches_total", w),
        "no_results": _current('knowledge_searches_total{outcome="no_results"}', w),
        "tokens": _current("assistant_llm_tokens_total", w),
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
