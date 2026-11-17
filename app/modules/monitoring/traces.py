"""Jaeger trace JSON to a `Failure` (system health spec §3). Pure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Failure:
    trace_id: str
    at: datetime
    service: str
    operation: str
    failed_step: str
    message: str
    #: Relative: the browser opens it under the same host, behind the tools login.
    trace_path: str


def _tags(span: dict) -> dict:
    return {t["key"]: t["value"] for t in span.get("tags", [])}


def _is_error(span: dict) -> bool:
    return _tags(span).get("error") in (True, "true")


def _message(span: dict) -> str:
    for log in span.get("logs", []):
        fields = {f["key"]: f["value"] for f in log.get("fields", [])}
        if fields.get("exception.message"):
            return str(fields["exception.message"])
    tags = _tags(span)
    return str(tags.get("otel.status_description") or "no error message recorded")


def to_failure(trace: dict) -> Failure | None:
    spans = trace.get("spans") or []
    errors = sorted((s for s in spans if _is_error(s)), key=lambda s: s["startTime"])
    if not errors:
        return None
    root = min(
        (s for s in spans if not s.get("references")), key=lambda s: s["startTime"],
        default=min(spans, key=lambda s: s["startTime"]),
    )
    first = errors[0]
    processes = trace.get("processes") or {}
    service = processes.get(root.get("processID"), {}).get("serviceName", "unknown")
    return Failure(
        trace_id=trace["traceID"],
        at=datetime.fromtimestamp(root["startTime"] / 1_000_000, UTC),
        service=service,
        operation=root["operationName"],
        failed_step=first["operationName"],
        message=_message(first),
        trace_path=f"/jaeger/trace/{trace['traceID']}",
    )
