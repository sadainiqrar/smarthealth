#!/usr/bin/env python
"""Claude Code `Stop` hook - the enforcement spine of the feature-test flow.

Contract with Claude Code:
    exit 0  -> allow the stop (no-op / satisfied / waived / peripheral)
    exit 2  -> BLOCK the stop; stderr is shown to the model and the user

Only the final step ever exits 2. Every other path - including any unexpected
internal error - exits 0. A Stop hook that blocks when it should not is the number
one reason a hook gets deleted, so this one fails open everywhere else.

Written in Python rather than bash so it behaves identically on Windows and POSIX.
The decision logic lives in `tests/runner/stop_gate.py` and is unit-tested; this
script only gathers inputs.

All output is ASCII: stderr is rendered by Claude Code, and a non-ASCII byte from a
cp1252 console does not round-trip as UTF-8.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ALLOW, BLOCK = 0, 2
REPO_NAME = "SmartHealth"

# route_check's documented exit codes.
ROUTE_CHECK_OK = 0
ROUTE_CHECK_INVALID = 1
ROUTE_CHECK_GATE_ERROR = 2


def run_git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=15, check=False, cwd=cwd
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def resolve_repo_root() -> Path | None:
    """The worktree toplevel, if this repo is SmartHealth (worktree-aware)."""
    toplevel = run_git("rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    root = Path(toplevel.strip())
    if root.name == REPO_NAME:
        return root
    common_dir = run_git("rev-parse", "--git-common-dir")
    if common_dir:
        canonical = Path(common_dir.strip()).resolve().parent
        if canonical.name == REPO_NAME:
            return root
    return None


def read_waiver(repo_root: Path) -> str | None:
    env_waiver = os.environ.get("E2E_WAIVE", "").strip()
    if env_waiver:
        return env_waiver
    waive_file = repo_root / ".e2e-waive"
    if waive_file.is_file():
        lines = waive_file.read_text(encoding="utf-8").splitlines()
        return (lines[0].strip() if lines else "") or "(no reason given)"
    return None


def log_waiver(repo_root: Path, reason: str, changed_files: list[str]) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean = reason.replace("\t", " ").replace("\n", " ")
    record = f"{timestamp}\t{clean}\t{','.join(changed_files)}\n"
    try:
        with (repo_root / "tests" / "waivers.log").open("a", encoding="utf-8") as handle:
            handle.write(record)
    except OSError:
        pass  # a waiver must never turn into a block


def dependencies_available() -> bool:
    """The dry-run needs pydantic and PyYAML. Without them, fail open."""
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import pydantic, yaml"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def route_check_exit_code(repo_root: Path) -> int:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "tests.runner.route_check", "--quiet"],
            cwd=repo_root, capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ROUTE_CHECK_GATE_ERROR
    return completed.returncode


def main() -> int:
    # Step 0 - guard. This hook may be wired globally; it must no-op everywhere else.
    repo_root = resolve_repo_root()
    if repo_root is None or not (repo_root / "tests" / "cases").is_dir():
        return ALLOW

    sys.path.insert(0, str(repo_root))
    from tests.runner.stop_gate import decide, load_globs, parse_porcelain

    porcelain = run_git("status", "--porcelain", "-z", cwd=repo_root)
    changed_files = parse_porcelain(porcelain or "")

    # Step 1 - waiver: an audited escape, never a silent one.
    waiver = read_waiver(repo_root)
    if waiver:
        log_waiver(repo_root, waiver, changed_files)
        return ALLOW

    # Step 2 - relevance.
    core_globs = load_globs(repo_root / "tests" / "core-paths.txt")

    # Step 3 - satisfied? A broken gate fails open; only an invalid case blocks.
    has_changed_case = any(
        path.startswith("tests/cases/") and path.endswith((".yaml", ".yml"))
        for path in changed_files
    )
    route_ok = True
    if core_globs and has_changed_case:
        if not dependencies_available():
            print(
                "[feature-test-stop] pydantic/PyYAML unavailable; skipping catalog "
                "validation and allowing the stop.",
                file=sys.stderr,
            )
        else:
            code = route_check_exit_code(repo_root)
            if code == ROUTE_CHECK_GATE_ERROR:
                print(
                    "[feature-test-stop] route_check could not run (exit 2); "
                    "allowing the stop rather than blocking on a broken gate.",
                    file=sys.stderr,
                )
            else:
                route_ok = code == ROUTE_CHECK_OK

    decision = decide(
        changed_files=changed_files,
        core_globs=core_globs,
        has_changed_case=has_changed_case,
        route_check_ok=route_ok,
    )
    if decision.allow:
        return ALLOW
    print(decision.message, file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - fail open on anything unexpected
        print(f"[feature-test-stop] internal error, allowing stop: {error}", file=sys.stderr)
        sys.exit(ALLOW)
