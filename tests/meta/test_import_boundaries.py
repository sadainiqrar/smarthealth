"""The import graph of `app`, enforced instead of merely documented.

Two independent rules live here, and conflating them is how a claim gets overstated in
front of a reviewer:

* **Vertical** -- the framework-free rule. Nothing outside the HTTP layer may reach
  FastAPI or Starlette. This is *layering*.
* **Horizontal** -- the module-boundary rule. `app/modules/<x>` may not import
  `app/modules/<y>`. This is *module separation*, and it is the property the phrase
  "modular monolith" actually names.

Everything down to `test_the_check_has_something_to_check` is the vertical rule; the
horizontal rule is at the bottom of the file.

--- The vertical rule ---

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


# --------------------------------------------------------------------------------- #
# The horizontal rule: modules do not import each other.
# --------------------------------------------------------------------------------- #
#
# This is the one the monolith argument rests on. A modular monolith and a plain
# monolith are identical on every externally visible axis -- one deployable, one
# codebase, one schema, one migration history. The *only* difference is whether the
# import graph has a deliberate shape, so "our boundaries are real" is precisely this
# assertion and nothing else.
#
# Until this existed the property was true but unguarded: `patients/service.py` could
# import `providers/service.py` and the entire suite stayed green. Week 2 is when that
# stops being hypothetical -- `scheduling/service.py` needs patient, provider, slot and
# clinic data, and a direct import is the path of least resistance and looks perfectly
# reasonable in a diff.
#
# Note what this rule does *not* touch: the database. `appointments` carries foreign
# keys into five other modules' tables and that is fine -- they are declared as table
# name strings, so there is no Python edge. Shared schema is the definition of a modular
# monolith, not a violation of it. The modularity lives in the import graph.

#: Cross-module imports that are allowed, as explicit (importer, imported) pairs.
#:
#: Pairs rather than a package-level allowlist, because "patients may use identity" is a
#: much broader permission than the one actually needed, and the broad version is how an
#: allowlist stops meaning anything. Every entry here is auth at the HTTP edge: the one
#: dependency nobody would extract.
CROSS_MODULE_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("app.modules.patients.router", "app.modules.identity.deps"),
        ("app.modules.patients.router", "app.modules.identity.models"),
        ("app.modules.patients.router", "app.modules.identity.security"),
        ("app.modules.providers.router", "app.modules.identity.deps"),
        ("app.modules.providers.router", "app.modules.identity.models"),
        ("app.modules.providers.router", "app.modules.identity.security"),
    }
)

#: Files that may never cross a module boundary, allowlist or not.
#:
#: The service layer is the unit of extraction and the models are what it owns, so an
#: edge here is the difference between "move three tables" and "find every call site".
#: The router is a composition point and is allowed a declared exception; these are not.
NEVER_CROSS = ("service", "models", "schemas")


def _owning_module(name: str) -> str | None:
    """`app.modules.patients.service` -> `app.modules.patients`.

    Returns None for anything that is not inside a domain module, including the
    `app.modules` package itself.
    """
    parts = name.split(".")
    if len(parts) >= 3 and parts[0] == "app" and parts[1] == "modules":
        return ".".join(parts[:3])
    return None


#: Every (importer, imported) edge that crosses from one domain module into another.
CROSS_MODULE_EDGES: list[tuple[str, str]] = sorted(
    (module, imported)
    for module in MODULES
    if (owner := _owning_module(module)) is not None
    for imported in IMPORTS[module][1]
    if (target := _owning_module(imported)) is not None and target != owner
)

MODULE_FILES = sorted(name for name in MODULES if _owning_module(name) is not None)


@pytest.mark.parametrize("module", MODULE_FILES)
def test_modules_do_not_import_each_other(module: str):
    """Rule 3: a cross-module import must be a declared exception, or it is a bug.

    The failure message names the alternatives rather than just refusing, because the
    right answer depends on what is being reached for: a published interface on the
    owning module, or a domain event once Week 3's Kafka exists. Reaching straight into
    another module's internals is the one option that quietly turns this codebase into a
    plain monolith with nobody deciding to.
    """
    undeclared = sorted(
        imported
        for importer, imported in CROSS_MODULE_EDGES
        if importer == module and (importer, imported) not in CROSS_MODULE_ALLOWED
    )
    assert not undeclared, (
        f"{module} imports {', '.join(undeclared)}, crossing a module boundary. "
        f"Modules stay independently extractable only while this graph has a shape: go "
        f"through a published interface on the owning module, or a domain event -- or, "
        f"if this edge is genuinely correct, add it to CROSS_MODULE_ALLOWED in this file "
        f"and say why in the commit."
    )


@pytest.mark.parametrize("module", [m for m in MODULE_FILES if m.rsplit(".", 1)[-1] in NEVER_CROSS])
def test_the_service_layer_never_crosses_a_module_boundary(module: str):
    """Rule 4: no exception exists for the layer that would be extracted.

    Stricter than rule 3 on purpose. CROSS_MODULE_ALLOWED is a router-layer escape
    hatch; if it ever grows an entry for a service, the allowlist would be granting
    exactly the coupling the boundary exists to prevent.
    """
    crossing = sorted(imported for importer, imported in CROSS_MODULE_EDGES if importer == module)
    assert not crossing, (
        f"{module} imports {', '.join(crossing)}. The service layer and the models it "
        f"owns are the unit of extraction, so they take no allowlist entry -- move the "
        f"dependency to the router, an interface, or an event."
    )


def test_the_cross_module_allowlist_has_no_stale_entries():
    """Keeps the allowlist honest, the same way FRAMEWORK_BOUND is kept honest.

    An entry for an edge that no longer exists silently pre-authorises coupling that
    nobody has argued for, and a list that is only ever appended to stops meaning
    anything.
    """
    stale = sorted(set(CROSS_MODULE_ALLOWED) - set(CROSS_MODULE_EDGES))
    assert not stale, (
        f"CROSS_MODULE_ALLOWED declares edges that no longer exist: {stale}. "
        f"Remove them so the exemption does not outlive its reason."
    )


def test_the_cross_module_check_has_something_to_check():
    """The empty-set failure mode: a green check that enumerated nothing."""
    owners = {_owning_module(name) for name in MODULE_FILES}
    assert len(owners) >= 4, f"expected at least four domain modules, found {sorted(owners)}"
    assert any(
        name.endswith(".service") for name in MODULE_FILES
    ), "found no service modules, so rule 4 asserted nothing"
