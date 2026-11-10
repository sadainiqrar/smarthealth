"""Every third-party package `app/` imports is a runtime dependency, not only a dev one.

Found the hard way (Week 3 slice 7): the billing gateway imported `httpx`, which was only
in the `dev` extra. Every test passed -- the test environment has dev dependencies -- and
the container image, which installs runtime dependencies only, failed on import. A test
whose environment is richer than production's cannot see this; this one reads the
declarations instead.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.meta


def _name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _runtime_closure() -> set[str]:
    """Declared runtime distributions and what they require (a module may come in
    through a dependency, e.g. kombu through celery)."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    seen: set[str] = set()
    queue = [(_name(r), set(re.findall(r"\[([^\]]+)\]", r)[0].split(",")) if "[" in r else set())
             for r in declared]
    while queue:
        name, extras = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for requirement in requires:
            marker = re.search(r'extra == "([^"]+)"', requirement)
            # A requested extra's requirements are runtime too (pyjwt[crypto] brings
            # cryptography); an unrequested extra's are not.
            if marker is None or marker.group(1) in {e.strip() for e in extras}:
                queue.append((_name(requirement), set()))
    return seen


def _third_party_imports() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return {m for m in found if m != "app" and m not in sys.stdlib_module_names}


def test_every_third_party_import_in_app_is_a_runtime_dependency():
    distributions = metadata.packages_distributions()
    runtime = _runtime_closure()
    missing = {
        module: distributions.get(module, ["?"])
        for module in _third_party_imports()
        if not {_name(d) for d in distributions.get(module, [])} & runtime
    }
    assert not missing, (
        f"app imports these, but no runtime dependency provides them: {missing}. "
        f"Add them to [project] dependencies in pyproject.toml, not only to the dev extra."
    )
