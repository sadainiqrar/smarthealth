#!/usr/bin/env python
"""Claude Code `Stop` hook - documentation must keep up with the application.

Contract with Claude Code:
    exit 0  -> allow the stop
    exit 2  -> BLOCK the stop; stderr is shown to the model and the user

Blocks when application code that the docs describe changed and no documentation changed
with it (rules: docs/doc-map.txt), or when the site's generated data no longer matches the
code (scripts/gen_docs_data.py --check). The change set is the working tree *plus* commits
on this branch that are not on origin/main yet, so committing the code does not slip past.

Fails open everywhere except the final decision, like feature_test_stop.py. The decision
logic is tests/runner/docs_gate.py, unit-tested in tests/tiers/t0_unit/test_docs_gate.py.
Waive with DOCS_WAIVE="<reason>" or a .docs-waive file; the waiver is logged.

All output is ASCII (stderr is rendered by Claude Code from a possibly-cp1252 console).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ALLOW, BLOCK = 0, 2
REPO_NAME = "SmartHealth"


def run_git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=15,
                              check=False, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def repo_root() -> Path | None:
    toplevel = run_git("rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    root = Path(toplevel.strip())
    if root.name == REPO_NAME:
        return root
    common = run_git("rev-parse", "--git-common-dir")
    if common and Path(common.strip()).resolve().parent.name == REPO_NAME:
        return root
    return None


def project_python(root: Path) -> str:
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def changed_files(root: Path) -> list[str]:
    from tests.runner.stop_gate import parse_porcelain

    files = set(parse_porcelain(run_git("status", "--porcelain", "-z", cwd=root) or ""))
    if run_git("rev-parse", "--verify", "--quiet", "origin/main", cwd=root):
        ahead = run_git("diff", "--name-only", "origin/main...HEAD", cwd=root) or ""
        files.update(line.strip() for line in ahead.splitlines() if line.strip())
    return sorted(files)


def data_stale(root: Path, files: list[str]) -> bool:
    """Only worth the few seconds when code the generator reads has changed."""
    relevant = ("app/", "services/", "migrations/")
    if not any(f.startswith(relevant) for f in files):
        return False
    try:
        done = subprocess.run([project_python(root), "scripts/gen_docs_data.py", "--check"],
                              cwd=root, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError):
        return False  # a gate that cannot run must not block
    return done.returncode == 1 and "stale:" in done.stdout


def waiver(root: Path) -> str | None:
    reason = os.environ.get("DOCS_WAIVE", "").strip()
    if reason:
        return reason
    waive_file = root / ".docs-waive"
    if waive_file.is_file():
        lines = waive_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return (lines[0].strip() if lines else "") or "(no reason given)"
    return None


def log_waiver(root: Path, reason: str, files: list[str]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean = reason.replace("\t", " ").replace("\n", " ")
    try:
        with (root / "tests" / "waivers.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t[docs] {clean}\t{','.join(files)}\n")
    except OSError:
        pass


def main() -> int:
    root = repo_root()
    if root is None or not (root / "docs" / "doc-map.txt").is_file():
        return ALLOW
    sys.path.insert(0, str(root))
    from tests.runner.docs_gate import decide, load_rules

    files = changed_files(root)
    reason = waiver(root)
    if reason:
        log_waiver(root, reason, files)
        return ALLOW
    decision = decide(changed_files=files, rules=load_rules(root / "docs" / "doc-map.txt"),
                      data_stale=data_stale(root, files))
    if decision.allow:
        return ALLOW
    print(f"[docs-stop] {decision.message}", file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - fail open on anything unexpected
        print(f"[docs-stop] internal error, allowing stop: {error}", file=sys.stderr)
        sys.exit(ALLOW)
