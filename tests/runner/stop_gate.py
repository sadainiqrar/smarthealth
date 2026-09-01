"""The `Stop` gate's decision logic — pure, deterministic, and unit-tested.

No git, no subprocess, no model, no network. `.claude/hooks/feature_test_stop.py`
gathers the inputs; this module decides. A gate whose logic cannot be tested is a
gate that gets ripped out the first time it misfires.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

BLOCK_TEMPLATE = (
    "Feature code changed in {files} but no validated test case.\n"
    "Run /feature-test to author one (and optionally run it),\n"
    "or set E2E_WAIVE=<reason> to skip with a logged reason."
)

INVALID_CASE_TEMPLATE = (
    "Feature code changed in {files} and a test case was touched, but the catalog "
    "did not validate.\n"
    "Run `python -m tests.runner.route_check` to see the errors, "
    "or set E2E_WAIVE=<reason> to skip with a logged reason."
)


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    message: str = ""
    waiver: str | None = None
    matched: tuple[str, ...] = ()


def parse_porcelain(raw: str) -> list[str]:
    """Extract changed paths from `git status --porcelain -z`.

    Rename and copy entries carry the NEW path first, then the origin path in the
    next NUL-delimited field; the origin is consumed and discarded.
    """
    fields = [field for field in raw.split("\0") if field]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        status, path = entry[:2], entry[3:]
        if path:
            paths.append(path)
        if status.startswith(("R", "C")):
            index += 1  # skip the origin path
        index += 1
    return paths


def load_globs(core_paths_file: Path) -> list[str]:
    """Read repo-root-relative globs, ignoring blank lines and full-line comments."""
    if not core_paths_file.is_file():
        return []
    globs = []
    for line in core_paths_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            globs.append(stripped)
    return globs


def matched_core_paths(changed_files: list[str], core_globs: list[str]) -> list[str]:
    """Changed files matching any core glob. `app/**` matches at every depth."""
    matched = []
    for path in changed_files:
        for glob in core_globs:
            if fnmatch.fnmatch(path, glob) or (
                glob.endswith("/**") and path.startswith(glob[:-2])
            ):
                matched.append(path)
                break
    return matched


def decide(
    *,
    changed_files: list[str],
    core_globs: list[str],
    has_changed_case: bool,
    route_check_ok: bool,
    waiver: str | None = None,
) -> Decision:
    """Return the gate's verdict. Every path but the last two allows the stop."""
    if waiver:
        return Decision(True, "waived", waiver=waiver)
    if not core_globs:
        return Decision(True, "no core paths configured")
    matched = matched_core_paths(changed_files, core_globs)
    if not matched:
        return Decision(True, "no core path changed")

    # ASCII only: this message is written to stderr for Claude Code to display, and a
    # U+2026 ellipsis encodes to cp1252 byte 0x85, which is not valid UTF-8.
    files = ", ".join(matched[:5]) + (" ..." if len(matched) > 5 else "")
    if not has_changed_case:
        return Decision(
            False, "core change with no case",
            message=BLOCK_TEMPLATE.format(files=files), matched=tuple(matched),
        )
    if not route_check_ok:
        return Decision(
            False, "case did not validate",
            message=INVALID_CASE_TEMPLATE.format(files=files), matched=tuple(matched),
        )
    return Decision(True, "covered by a validated case", matched=tuple(matched))
