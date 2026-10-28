"""Only scheduling/service.py may update a slot's status.

R-2: a slot's status and its appointment's status are two independently writable facts,
and the database cannot keep them in agreement. The service keeps every slot update
inside the transaction that changes the appointment. A second place that issues
`update(ProviderSlot)` is a second place that can forget the other half.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
ALLOWED = APP_ROOT / "modules" / "scheduling" / "service.py"


def _updates_provider_slot(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "update"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "ProviderSlot"
        for node in ast.walk(tree)
    )


def test_only_the_scheduling_service_updates_slots():
    offenders = [
        str(path.relative_to(APP_ROOT.parent))
        for path in APP_ROOT.rglob("*.py")
        if path != ALLOWED and _updates_provider_slot(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"slot status updated outside scheduling/service.py: {offenders}"


def test_the_guard_sees_the_service():
    assert _updates_provider_slot(ast.parse(ALLOWED.read_text(encoding="utf-8")))
