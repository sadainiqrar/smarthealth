"""Response models for GET /monitoring/* (system health spec §4.1, §4.2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["ok", "warn", "bad", "idle", "unknown"]
SourceStatus = Literal["ok", "unavailable", "not_configured"]


class Sources(BaseModel):
    prometheus: SourceStatus
    jaeger: SourceStatus


class Component(BaseModel):
    name: str
    job: str
    status: Status
    reason: str


class FlowRead(BaseModel):
    key: str
    label: str
    total: int | None
    failed: int | None
    failure_pct: float | None
    p95_seconds: float | None
    previous_total: int | None
    status: Status
    reason: str


class Backlog(BaseModel):
    key: str
    label: str
    value: float | None
    status: Status
    reason: str


class AiHealth(BaseModel):
    turns: int | None
    errors: int | None
    error_pct: float | None
    first_token_p95_seconds: float | None
    retrieval_no_results_pct: float | None
    tokens: int | None
    status: Status
    reason: str


class Overview(BaseModel):
    window_hours: int
    generated_at: datetime
    sources: Sources
    components: list[Component]
    flows: list[FlowRead]
    backlogs: list[Backlog]
    ai: AiHealth


class FailureRead(BaseModel):
    trace_id: str
    at: datetime
    service: str
    operation: str
    failed_step: str
    message: str
    trace_path: str


class FailuresRead(BaseModel):
    window_hours: int
    source: SourceStatus
    failures: list[FailureRead]
