"""Every Temporal workflow and activity in the source tree is registered, and every
registered workflow is valid inside Temporal's sandbox.

Discovery reads the source with `ast` rather than trusting the registry, because the
failure being guarded against is exactly a definition the registry does not know
about: a workflow no worker runs, whose starts sit in the queue forever.

Sandbox validation catches a workflow module doing non-deterministic work at import
(reading the clock, opening a socket). It cannot see what `run` does at runtime --
the T2 tier covers that, because every T2 test executes inside the same sandbox.

Discovery only recognises the literal spellings `@workflow.defn` and `@activity.defn`,
so `test_temporal_is_imported_only_in_the_discoverable_form` forbids every import that
would evade it. The two tests together make discovery complete: the spelling is
enforced, and anything spelled that way is found.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Private API, used knowingly: it is the only offline entry to the validation a Worker
# performs at construction. Verified on temporalio 1.34. If an upgrade removes it,
# build a Worker against the T2 time-skipping environment instead.
from temporalio.workflow import _Definition

from app.components import register_all
from app.core.temporal import ACTIVITIES, WORKFLOWS

pytestmark = pytest.mark.meta

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _is_defn(decorator: ast.expr, namespace: str) -> bool:
    """Matches `@workflow.defn`, `@workflow.defn(...)`, and the activity equivalents."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "defn"
        and isinstance(target.value, ast.Name)
        and target.value.id == namespace
    )


def _discover(namespace: str, node_types: tuple[type, ...]) -> set[str]:
    found: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        module = ".".join(path.relative_to(APP_ROOT.parent).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for node in ast.iter_child_nodes(parent):
                if isinstance(node, node_types) and any(
                    _is_defn(d, namespace) for d in node.decorator_list
                ):
                    owner = f"{parent.name}." if isinstance(parent, ast.ClassDef) else ""
                    found.add(f"{module}.{owner}{node.name}")
    return found


def _undiscoverable_temporal_imports(tree: ast.AST) -> list[str]:
    """Imports of Temporal's decorator namespaces that `_is_defn` cannot see through."""
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [
                f"import {a.name}"
                for a in node.names
                if a.name in ("temporalio", "temporalio.workflow", "temporalio.activity")
            ]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in ("temporalio.workflow", "temporalio.activity"):
                bad.append(f"from {node.module} import ...")
            elif node.module == "temporalio":
                bad += [
                    f"from temporalio import {a.name} as {a.asname}"
                    for a in node.names
                    if a.name in ("workflow", "activity") and a.asname
                ]
    return bad


def _qualified(item) -> str:
    target = getattr(item, "__func__", item)  # unwrap bound methods
    return f"{target.__module__}.{target.__qualname__}"


register_all()


def test_every_workflow_definition_is_registered():
    discovered = _discover("workflow", (ast.ClassDef,))
    registered = {_qualified(cls) for cls in WORKFLOWS}
    assert discovered, "found no @workflow.defn at all -- has the layout changed?"
    assert discovered == registered, (
        f"unregistered: {sorted(discovered - registered)}; "
        f"registered but not found in source: {sorted(registered - discovered)}. "
        f"Register workflows in app/components.py."
    )


def test_every_activity_definition_is_registered():
    discovered = _discover("activity", (ast.FunctionDef, ast.AsyncFunctionDef))
    registered = {_qualified(fn) for fn in ACTIVITIES}
    assert discovered == registered, (
        f"unregistered: {sorted(discovered - registered)}; "
        f"registered but not found in source: {sorted(registered - discovered)}. "
        f"Register activities in app/components.py."
    )


def test_temporal_is_imported_only_in_the_discoverable_form():
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"{path.relative_to(APP_ROOT.parent).as_posix()}: {found}"
            for found in _undiscoverable_temporal_imports(tree)
        ]
    assert not offenders, (
        f"{offenders}. The registry discovery matches the literal names `workflow` and "
        f"`activity`, so a workflow or activity imported any other way (`from "
        f"temporalio.workflow import defn`, an alias, `import temporalio`) goes "
        f"undiscovered and the registration tests stay green while it is unregistered. "
        f"Use `from temporalio import workflow` / `from temporalio import activity`."
    )


def test_registered_workflow_type_names_are_unique():
    """Registry keys are class names, but Temporal routes on the *type name*, which
    `@workflow.defn(name=...)` can override -- two classes sharing one would shadow
    each other at the worker."""
    names = [_Definition.must_from_class(cls).name for cls in WORKFLOWS]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate Temporal workflow type names: {duplicates}"


def test_register_all_is_idempotent():
    """The worker and the tests may both call it; a second call must not raise."""
    before = (len(WORKFLOWS), len(ACTIVITIES))
    register_all()
    assert (len(WORKFLOWS), len(ACTIVITIES)) == before


@pytest.mark.parametrize("workflow_class", list(WORKFLOWS), ids=lambda cls: cls.__name__)
async def test_registered_workflows_pass_the_sandbox(workflow_class):
    # async only because creating the sandboxed instance calls get_running_loop();
    # nothing is awaited and no server is involved.
    SandboxedWorkflowRunner().prepare_workflow(_Definition.must_from_class(workflow_class))
