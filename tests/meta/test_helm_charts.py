"""Render-time invariants of the Helm charts (spec 2026-11-17-helm-charts-k3d-design.md).

Every test renders the umbrella with `helm template`; nothing needs a cluster. Skipped when
helm is not installed, so the fast suite stays green on a machine without it.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.meta

ROOT = Path(__file__).resolve().parents[2]
HELM_DIR = ROOT / "deploy" / "helm"
UMBRELLA = HELM_DIR / "smarthealth"
HELM = shutil.which("helm")

needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*args: str) -> str:
    result = subprocess.run([HELM, *args], capture_output=True, text=True, cwd=ROOT,
                            check=False)
    if result.returncode != 0:
        raise AssertionError(f"helm {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


@lru_cache(maxsize=8)
def render(*value_files: str, sets: tuple[str, ...] = ()) -> tuple[dict, ...]:
    _helm("dependency", "build", UMBRELLA.as_posix())
    args = ["template", "smarthealth", UMBRELLA.as_posix(), "--namespace", "smarthealth"]
    for name in value_files:
        args += ["-f", (UMBRELLA / name).as_posix()]
    for item in sets:
        args += ["--set", item]
    return tuple(doc for doc in yaml.safe_load_all(_helm(*args)) if doc)


def of_kind(docs, kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


@needs_helm
def test_the_umbrella_renders():
    render("values-local.yaml")
