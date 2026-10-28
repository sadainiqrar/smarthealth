"""Every Temporal workflow and activity in the source tree is registered, and every
registered workflow is valid inside Temporal's sandbox.

Discovery reads the source with `ast` rather than trusting the registry, because the
failure being guarded against is exactly a definition the registry does not know
about: a workflow no worker runs, whose starts sit in the queue forever.

Sandbox validation catches a workflow module doing non-deterministic work at import
(reading the clock, opening a socket). It cannot see what `run` does at runtime --
the T2 tier covers that, because every T2 test executes inside the same sandbox.
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
