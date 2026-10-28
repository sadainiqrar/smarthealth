"""Only scheduling/service.py may write a slot's status.

R-2: a slot's status and its appointment's status are two independently writable facts,
and the database cannot keep them in agreement. The service keeps every slot update
inside the transaction that changes the appointment. A second place that writes slot
status is a second place that can forget the other half.

The guard scans `app/**/*.py` outside the service and flags every route to a slot-status
write it knows of:

- a call named `update` -- bare, an attribute such as `sa.update`, or an alias from
  `from sqlalchemy... import update as X` -- given an argument ending in `ProviderSlot`;
- `<...>ProviderSlot.__table__.update`, Core's spelling of the same statement;
- a string constant naming both `provider_slots` and `update`, i.e. raw SQL;
- an assignment (`=`, `+=`, annotated) to an attribute named `status`, in a file that
  imports or references `ProviderSlot` or `SlotStatus` -- the ORM's spelling.

The constructor keyword `ProviderSlot(..., status=...)` is an insert, not a write to an
existing slot, and stays allowed: `app/seed.py` generates slots that way. The rules are
syntactic and deliberately broad; a false positive is fixed by moving the write into the
service, which is the point.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
ALLOWED = APP_ROOT / "modules" / "scheduling" / "service.py"

SLOT_NAMES = frozenset({"ProviderSlot", "SlotStatus"})


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_provider_slot(node: ast.AST) -> bool:
    name = _terminal_name(node)
    return name is not None and name.endswith("ProviderSlot")


def _update_aliases(tree: ast.AST) -> set[str]:
    """`update` plus every local name SQLAlchemy's `update` was imported under."""
    names = {"update"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            names.update(a.asname or a.name for a in node.names if a.name == "update")
    return names


def _mentions_slot_names(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name in SLOT_NAMES for a in node.names):
            return True
        if _terminal_name(node) in SLOT_NAMES:
            return True
    return False


def _targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        pending = list(node.targets)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        pending = [node.target]
    else:
        return []
    flat: list[ast.AST] = []
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Tuple | ast.List):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            flat.append(target)
    return flat


def slot_status_writes(source: str) -> list[str]:
    """Every slot-status write the guard can see in `source`, one line each."""
    tree = ast.parse(source)
    update_names = _update_aliases(tree)
    status_assignment_counts = _mentions_slot_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id in update_names)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "update")
            )
            and any(_is_provider_slot(arg) for arg in node.args)
        ):
            found.append(f"line {line}: update(ProviderSlot)")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "update"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "__table__"
            and _is_provider_slot(node.value.value)
        ):
            found.append(f"line {line}: ProviderSlot.__table__.update")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            if "provider_slots" in text and "update" in text:
                found.append(f"line {line}: raw SQL updating provider_slots")
        elif status_assignment_counts and any(
            isinstance(t, ast.Attribute) and t.attr == "status" for t in _targets(node)
        ):
            found.append(f"line {line}: assignment to .status")
    return found


def test_only_the_scheduling_service_writes_slot_status():
    offenders = {
        str(path.relative_to(APP_ROOT.parent)): writes
        for path in APP_ROOT.rglob("*.py")
        if path != ALLOWED
        and (writes := slot_status_writes(path.read_text(encoding="utf-8")))
    }
    assert not offenders, f"slot status written outside scheduling/service.py: {offenders}"


def test_the_guard_sees_the_service():
    assert slot_status_writes(ALLOWED.read_text(encoding="utf-8"))


_IMPORTS = "from app.modules.scheduling.models import ProviderSlot, SlotStatus\n"


@pytest.mark.parametrize(
    "source",
    [
        "update(ProviderSlot).values(status='free')",
        "import sqlalchemy as sa\nsa.update(ProviderSlot)",
        "from sqlalchemy import update as upd\nupd(ProviderSlot)",
        "from sqlalchemy.sql.expression import update as u\nu(ProviderSlot)",
        "update(models.ProviderSlot)",
        "ProviderSlot.__table__.update().values(status='free')",
        "models.ProviderSlot.__table__.update()",
        "text('UPDATE provider_slots SET status = :s')",
        "SQL = '''\n  update Provider_Slots set status = 'free'\n'''",
        _IMPORTS + "slot.status = SlotStatus.FREE",
        _IMPORTS + "slot.status += ''",
        _IMPORTS + "slot.status: SlotStatus = SlotStatus.FREE",
        _IMPORTS + "other, slot.status = 1, SlotStatus.FREE",
        "from app.modules.scheduling import models\nslot.status = models.SlotStatus.FREE",
    ],
    ids=[
        "bare-update", "sa-update", "aliased-update", "aliased-from-submodule",
        "qualified-model", "core-table-update", "qualified-core-table-update", "raw-sql",
        "raw-sql-mixed-case", "orm-assign", "orm-augassign", "orm-annassign",
        "orm-tuple-assign", "orm-assign-qualified-reference",
    ],
)
def test_the_guard_flags_every_known_bypass(source):
    assert slot_status_writes(source), f"not flagged:\n{source}"


@pytest.mark.parametrize(
    "source",
    [
        _IMPORTS + "session.add(ProviderSlot(provider_id=p, status=SlotStatus.FREE))",
        _IMPORTS + "select(ProviderSlot).where(ProviderSlot.status == SlotStatus.FREE)",
        "appointment.status = 'cancelled'",  # no slot names in the file
    ],
    ids=["seed-style-constructor", "read-only-query", "status-in-an-unrelated-file"],
)
def test_the_guard_allows_what_is_not_a_slot_write(source):
    assert not slot_status_writes(source)
