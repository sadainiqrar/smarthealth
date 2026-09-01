"""The case schema — the validation authority for `tests/cases/*.yaml`.

Every rule in the design spec (section 7.3) is enforced here and nowhere else. The
most important one is the anti-stub rule: a case that asserts nothing must be a
hard error, never a silent skip.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

KEBAB_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

Tier = Literal["unit", "contract", "workflow", "integration", "journey"]
Priority = Literal["P0", "P1", "P2"]
Status = Literal["ready", "blocked", "stub"]

#: Every step kind the DSL knows about. The engine implements a subset (see
#: ``tests/runner/discover.SUPPORTED_STEP_KINDS``); the rest are authorable now and
#: fail loudly rather than silently until their phase lands.
STEP_KINDS: tuple[str, ...] = ("api", "emit", "await", "advance_time", "chaos", "ai")


class CaseValidationError(Exception):
    """A case file could not be parsed, or violates the schema."""


class ApiExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int | None = None
    json_contains: dict[str, Any] | None = None


class ApiStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str
    role: str | None = Field(default=None, alias="as")
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    expect: ApiExpect | None = None


class Step(BaseModel):
    """One journey step. Exactly one kind may be set."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api: ApiStep | None = None
    emit: dict[str, Any] | None = None
    wait_for: dict[str, Any] | None = Field(default=None, alias="await")
    advance_time: str | None = None
    chaos: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None

    def _value(self, kind: str) -> Any:
        return getattr(self, "wait_for" if kind == "await" else kind)

    @property
    def kind(self) -> str:
        return next(k for k in STEP_KINDS if self._value(k) is not None)

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> Step:
        set_kinds = [k for k in STEP_KINDS if self._value(k) is not None]
        if len(set_kinds) != 1:
            raise ValueError(
                f"a step must set exactly one of {list(STEP_KINDS)}; "
                f"got {set_kinds if set_kinds else 'none'}"
            )
        return self


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: str | None = None
    env: dict[str, str] | None = None


class DbExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int | None = None
    where: dict[str, Any] | None = None


class EventExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    count: int | None = None
    key: str | None = None


class TraceExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span: str
    children: list[str] = Field(default_factory=list)
    status: Literal["ok", "error"] | None = None


class MetricExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    delta: float | None = None


class Expect(BaseModel):
    """Post-journey expectations. Kinds are implemented phase by phase."""

    model_config = ConfigDict(extra="forbid")

    api: ApiExpect | None = None
    db: dict[str, DbExpect] | None = None
    events: list[EventExpect] | None = None
    traces: list[TraceExpect] | None = None
    metrics: list[MetricExpect] | None = None
    invariants: list[str] | None = None

    @property
    def kinds(self) -> set[str]:
        # `type(self).model_fields` — accessing model_fields on an instance is
        # deprecated in pydantic 2.11+.
        return {name for name in type(self).model_fields if getattr(self, name) is not None}


class Judge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    rubrics: list[Literal["groundedness", "safety", "task_success"]] = Field(
        default_factory=list
    )


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    requirement: list[str] = Field(default_factory=list)
    tier: Tier
    priority: Priority = "P1"
    status: Status = "ready"
    blocked_on: str | None = None
    setup: Setup | None = None
    steps: list[Step] = Field(default_factory=list)
    expect: Expect | None = None
    chaos: dict[str, Any] | None = None
    judge: Judge | None = None
    impl: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_is_kebab_case(cls, value: str) -> str:
        if not KEBAB_CASE.match(value):
            raise ValueError(f"id '{value}' must be kebab-case (lowercase, digits, hyphens)")
        return value

    @field_validator("requirement", mode="before")
    @classmethod
    def _coerce_requirement(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Case:
        if self.status == "blocked" and not self.blocked_on:
            raise ValueError("status 'blocked' requires blocked_on: '<reason>'")
        if self.blocked_on and self.status != "blocked":
            raise ValueError("blocked_on may only be set when status is 'blocked'")
        if self.status != "blocked" and not self.steps and not self.impl:
            raise ValueError(
                "anti-stub: a case must declare `steps` or `impl`, or be "
                "status: blocked with a blocked_on reason. A case that asserts "
                "nothing must never look automated."
            )
        if self.status != "blocked" and not self.impl and not self._has_assertion():
            raise ValueError(
                "anti-stub: this case declares steps but asserts nothing. Add a "
                "case-level `expect:`, a per-step `expect:`, or an `await` step. "
                "A case that runs without asserting is worse than no case."
            )
        has_ai_step = any(step.kind == "ai" for step in self.steps)
        if has_ai_step and self.judge is None:
            raise ValueError("a case with an `ai` step requires a `judge` block")
        if self.judge is not None and not has_ai_step:
            raise ValueError("a `judge` block is only meaningful on a case with an `ai` step")
        return self

    @property
    def step_kinds(self) -> set[str]:
        return {step.kind for step in self.steps}

    def _has_assertion(self) -> bool:
        """Whether this case checks anything at all.

        A per-step `expect`, a case-level `expect`, or an `await` step (which fails
        on timeout) all count. Nothing else does — emitting an event or calling an
        endpoint without checking the outcome asserts nothing.
        """
        if self.expect is not None:
            return True
        for step in self.steps:
            if step.kind == "await":
                return True
            if step.kind == "api" and step.api is not None and step.api.expect is not None:
                return True
        return False

    @classmethod
    def from_file(cls, path: Path) -> Case:
        """Load and validate one case file. Raises `CaseValidationError` on any problem."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CaseValidationError(f"cannot read case file: {exc}") from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise CaseValidationError(f"invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise CaseValidationError("expected a YAML mapping at the top level")
        try:
            case = cls.model_validate(raw)
        except ValidationError as exc:
            raise CaseValidationError(_format_validation_error(exc)) from exc
        if case.id != path.stem:
            raise CaseValidationError(
                f"id '{case.id}' must equal the filename stem '{path.stem}'"
            )
        return case


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
