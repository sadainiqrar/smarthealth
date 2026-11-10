"""The assistant's metrics, for the Grafana *Assistant* dashboard (PART-B-FR-5, OBS).

Recorded in one place -- when a turn (or a report) finishes -- from the same `TurnUsage`
the usage page reads, so the dashboard and the page cannot disagree. Exported over OTLP to
the shared collector, which Prometheus scrapes (`job="smarthealth/assistant"`).

**Labels stay low-cardinality**: role, outcome, model, draft kind and status -- never a
user, conversation or turn id. Per-user detail is the usage page's (assistant-db); a label
per user would make every series multiply with the user count.

The instruments come from the global meter: until `telemetry.setup` installs a provider
they are no-ops, so tests and an un-exported process pay nothing.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("assistant")

turns = _meter.create_counter(
    "assistant.turns",
    unit="1",
    description="Finished turns, by role, outcome and whether the model was used",
)
turn_duration = _meter.create_histogram(
    "assistant.turn.duration", unit="ms", description="Whole turn, first event to done"
)
first_token = _meter.create_histogram(
    "assistant.first_token", unit="ms", description="Time to the model's first token"
)
llm_calls = _meter.create_counter(
    "assistant.llm.calls", unit="1", description="Calls to the language model"
)
llm_tokens = _meter.create_counter(
    "assistant.llm.tokens", unit="1", description="Tokens, by direction (input/output)"
)
drafts = _meter.create_counter(
    "assistant.drafts", unit="1", description="Drafts, by kind and status"
)
bookings = _meter.create_counter(
    "assistant.bookings",
    unit="1",
    description="Bookings seen on appointments.booked, by origin (assistant or not)",
)
reports = _meter.create_counter(
    "assistant.reports", unit="1", description="Reports, by kind and outcome"
)


def record_turn(
    *,
    role: str,
    outcome: str,
    model: str,
    model_calls: int,
    input_tokens: int,
    output_tokens: int,
    first_token_ms: int | None,
    total_ms: int,
    kind: str = "chat",
) -> None:
    used = "true" if model_calls else "false"
    base = {"role": role, "model_used": used, "kind": kind}
    turns.add(1, {**base, "outcome": outcome})
    turn_duration.record(total_ms, base)
    if first_token_ms is not None:
        first_token.record(first_token_ms, {"role": role, "kind": kind})
    if model_calls:
        llm_calls.add(model_calls, {"model": model, "kind": kind})
        llm_tokens.add(input_tokens, {"model": model, "direction": "input"})
        llm_tokens.add(output_tokens, {"model": model, "direction": "output"})


def record_draft(kind: str, status: str) -> None:
    drafts.add(1, {"kind": kind, "status": status})


def record_report(kind: str, status: str) -> None:
    reports.add(1, {"kind": kind, "status": status})


def record_booking(origin: str) -> None:
    bookings.add(1, {"origin": "assistant" if origin == "assistant" else "other"})
