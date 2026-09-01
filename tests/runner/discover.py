"""Load, validate, and route the case catalog.

Routing decides *how* a case executes, not whether it is valid — validity is
`schema.Case`'s job. A case lands in exactly one bucket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tests.runner.schema import Case, CaseValidationError

CASES_DIR = Path(__file__).resolve().parents[1] / "cases"

#: Step kinds the engine can execute today. Everything else is authorable but will
#: fail loudly when run — see `engine.run_case`. Extend this as phases land.
SUPPORTED_STEP_KINDS: set[str] = {"api"}

#: Expectation kinds the engine can assert today.
SUPPORTED_EXPECT_KINDS: set[str] = {"api"}


@dataclass(frozen=True)
class CaseError:
    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path.name}: {self.message}"


@dataclass
class LoadResult:
    cases: list[Case] = field(default_factory=list)
    errors: list[CaseError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Routing:
    """Cases bucketed by execution strategy. Every loaded case appears exactly once."""

    blocked: list[Case] = field(default_factory=list)
    unsupported: list[Case] = field(default_factory=list)
    impl_backed: list[Case] = field(default_factory=list)
    declarative: list[Case] = field(default_factory=list)

    @property
    def all_cases(self) -> list[Case]:
        return self.blocked + self.unsupported + self.impl_backed + self.declarative


def load_cases(cases_dir: Path = CASES_DIR) -> LoadResult:
    """Load every `*.yaml` / `*.yml` under `cases_dir`, collecting errors rather than raising."""
    result = LoadResult()
    if not cases_dir.is_dir():
        return result
    seen: dict[str, Path] = {}
    paths = sorted([*cases_dir.glob("*.yaml"), *cases_dir.glob("*.yml")], key=lambda p: p.name)
    for path in paths:
        try:
            case = Case.from_file(path)
        except CaseValidationError as exc:
            result.errors.append(CaseError(path, str(exc)))
            continue
        if case.id in seen:
            result.errors.append(
                CaseError(path, f"duplicate id '{case.id}' (also declared in {seen[case.id].name})")
            )
            continue
        seen[case.id] = path
        result.cases.append(case)
    result.cases.sort(key=lambda case: case.id)
    return result


def unsupported_kinds(case: Case) -> set[str]:
    """Step and expectation kinds this case needs that the engine cannot run yet."""
    missing = case.step_kinds - SUPPORTED_STEP_KINDS
    if case.expect is not None:
        missing |= case.expect.kinds - SUPPORTED_EXPECT_KINDS
    return missing


def route(cases: list[Case]) -> Routing:
    """Bucket cases by execution strategy, in precedence order."""
    routing = Routing()
    for case in cases:
        if case.status == "blocked":
            routing.blocked.append(case)
        elif unsupported_kinds(case):
            routing.unsupported.append(case)
        elif case.impl:
            routing.impl_backed.append(case)
        else:
            routing.declarative.append(case)
    return routing
