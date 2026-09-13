"""The framework-free rule, enforced instead of merely documented.

`app/core/**`, `app/db/constraints.py` and every `service.py` state in their docstrings
that importing them must not drag FastAPI, Starlette or the ASGI stack into a process
that has no business loading them -- a Temporal worker, a Celery task, a plain script.
Until this file existed, that held by discipline alone: a single
`from fastapi import HTTPException` inside a service passed the entire suite, and the
cost would have landed in Week 2 when a Temporal worker began importing Starlette.

Two things are checked, and the second is the one a reviewer will not think of:

1. **Direct imports.** No framework-free module imports `fastapi` or `starlette`.
2. **Transitive imports.** No framework-free module imports an *application* module
   that is itself framework-bound. `from app.api.deps import get_audit_log` inside a
   service pulls FastAPI in exactly as surely as importing it directly, and a
   direct-import check waves it through.

Imports are read with `ast`, never by importing the modules: a test that must import
the code to check it cannot report on code that fails to import.

Adding a module to `FRAMEWORK_BOUND` is how you declare an exception, and the list is
checked for staleness in both directions -- so the declaration stays visible in a diff
rather than quietly becoming the norm.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"

#: Import these and you have a web framework in your process. That is the whole point.
FRAMEWORK_PACKAGES = frozenset({"fastapi", "starlette"})

#: The modules allowed to be framework-bound: the HTTP layer, and only the HTTP layer.
#:
#: This is an allowlist rather than a denylist deliberately. A new module is
#: framework-free **by default**, so the rule covers code nobody remembered to think
#: about -- and adding a name here is a visible line in a diff, which is where a
#: decision like this should be argued.
FRAMEWORK_BOUND: frozenset[str] = frozenset(
    {
        "app.main",
        "app.api.router",
        "app.api.deps",
        "app.api.health",
        "app.api.error_handlers",
        "app.modules.identity.deps",
        "app.modules.identity.router",
        "app.modules.patients.router",
        "app.modules.providers.router",
    }
)


def _module_name(path: Path) -> str:
    """`app/core/errors.py` -> `app.core.errors`; a package `__init__` -> the package."""
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _all_modules() -> dict[str, Path]:
    return {
        _module_name(path): path
        for path in sorted(APP_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


MODULES = _all_modules()


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """Every module this file imports, split into (external roots, internal app modules).

    `from app.modules.patients import service` is ambiguous in the AST: `service` could
    be a submodule or a name inside the package. Both candidates are resolved against
    the real module set, and only ones that exist are kept -- so the check neither
    misses a submodule import nor invents an edge from an ordinary symbol import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    external: set[str] = set()
    internal: set[str] = set()

    def record(candidate: str) -> None:
        if candidate in MODULES:
            internal.add(candidate)
        elif not candidate.startswith("app"):
            external.add(candidate.split(".")[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:  # relative imports are not used here
                continue
            record(node.module)
            for alias in node.names:
                record(f"{node.module}.{alias.name}")

    return external, internal


IMPORTS = {name: _imports(path) for name, path in MODULES.items()}
FRAMEWORK_FREE = sorted(set(MODULES) - FRAMEWORK_BOUND)


@pytest.mark.parametrize("module", FRAMEWORK_FREE)
def test_framework_free_modules_do_not_import_a_web_framework(module: str):
    """Rule 1: no direct `fastapi` or `starlette` import outside the HTTP layer."""
    external, _internal = IMPORTS[module]
    offending = sorted(external & FRAMEWORK_PACKAGES)
    assert not offending, (
        f"{module} imports {', '.join(offending)}. Modules outside the HTTP layer must "
        f"stay importable by a Temporal worker or Celery task, which has no ASGI stack. "
        f"Raise a DomainError from app.core.errors and let app/api/error_handlers.py "
        f"translate it -- or, if this module genuinely belongs to the HTTP layer, add it "
        f"to FRAMEWORK_BOUND in this file and say why in the commit."
    )


@pytest.mark.parametrize("module", FRAMEWORK_FREE)
def test_framework_free_modules_do_not_reach_a_framework_through_another_module(
    module: str,
):
    """Rule 2: the transitive case, which a direct-import check waves through.

    Walks the application import graph from this module and fails if it can reach any
    framework-bound module. The failure message prints the actual path, because
    "something you import imports FastAPI" is not an actionable error message.
    """
    seen: set[str] = {module}
    queue: deque[tuple[str, list[str]]] = deque([(module, [module])])

    while queue:
        current, path = queue.popleft()
        for imported in sorted(IMPORTS[current][1]):
            if imported in seen:
                continue
            trail = [*path, imported]
            assert imported not in FRAMEWORK_BOUND, (
                f"{module} reaches the HTTP layer: {' -> '.join(trail)}. "
                f"{imported} imports a web framework, so importing {module} loads it "
                f"too -- which defeats the point of keeping {module} framework-free."
            )
            seen.add(imported)
            queue.append((imported, trail))


def test_every_framework_bound_module_actually_imports_a_framework():
    """Keeps the allowlist honest in the other direction.

    An entry that no longer imports a framework is a stale exemption: it silently
    exempts a module that has since become clean, so a future regression there would
    pass unnoticed. Lists that are only ever appended to stop meaning anything.
    """
    stale = sorted(
        name
        for name in FRAMEWORK_BOUND
        if name in IMPORTS and not (IMPORTS[name][0] & FRAMEWORK_PACKAGES)
    )
    assert not stale, (
        f"FRAMEWORK_BOUND lists {stale}, which no longer import a web framework. "
        f"Remove them so the exemption does not outlive its reason."
    )


def test_the_allowlist_names_only_modules_that_exist():
    """A renamed or deleted module leaves a dead entry that exempts nothing, and hides
    the fact that its replacement is now unguarded."""
    missing = sorted(FRAMEWORK_BOUND - set(MODULES))
    assert not missing, f"FRAMEWORK_BOUND names modules that do not exist: {missing}"


def test_the_check_has_something_to_check():
    """Guards against the whole file silently passing because the glob matched nothing
    -- the failure mode where a meta-test reports green while enumerating an empty set."""
    assert len(FRAMEWORK_FREE) > 20, f"only found {len(FRAMEWORK_FREE)} framework-free modules"
    assert FRAMEWORK_BOUND, "expected at least one framework-bound module"
