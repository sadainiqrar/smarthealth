"""The docs `Stop` gate's decision logic -- pure, deterministic, unit-tested.

`.claude/hooks/docs_stop.py` gathers the inputs (changed files, whether the generated docs
data is stale); this module decides. Same shape as `stop_gate.py`: a gate whose logic
cannot be tested is a gate that gets ripped out the first time it misfires.

The rule: application code that the docs describe changed, and no documentation changed
with it -> block, naming the docs to review. Documentation is any file in DOC_GLOBS.
Generated site data that no longer matches the code -> block, naming the command.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

#: What counts as having updated the documentation.
DOC_GLOBS = (
    "README.md", "CLAUDE.md", "docs/*.md", "docs/index.html", "docs/assets/**",
    "docs/data/**", "services/*/README.md", "tests/README.md", "frontend/README.md",
)

BLOCK_TEMPLATE = (
    "Application code changed but no documentation did:\n{groups}\n"
    "Review and update the docs named above if this change affects what they describe\n"
    "(the site is docs/index.html; generated data via `python scripts/gen_docs_data.py`),\n"
    "or set DOCS_WAIVE=<reason> to skip with a logged reason (e.g. a pure refactor)."
)

STALE_TEMPLATE = (
    "The documentation site's generated data (docs/data/*.json) no longer matches the\n"
    "code. Run `python scripts/gen_docs_data.py` and commit the result,\n"
    "or set DOCS_WAIVE=<reason> to skip with a logged reason."
)


@dataclass(frozen=True)
class Rule:
    glob: str
    docs: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    message: str = ""


def _matches(path: str, glob: str) -> bool:
    """`dir/**` matches at any depth; otherwise `*` stays within one directory level
    (fnmatch's own `*` crosses "/", so `docs/*.md` would match `docs/a/b/c.md`)."""
    if glob.endswith("/**"):
        return path.startswith(glob[:-2])
    return fnmatch.fnmatch(path, glob) and path.count("/") == glob.count("/")


def load_rules(map_file: Path) -> list[Rule]:
    """Read `<glob> -> <doc>, <doc>` lines; blank lines and comments are ignored."""
    if not map_file.is_file():
        return []
    rules = []
    for line in map_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "->" not in stripped:
            continue
        glob, docs = (part.strip() for part in stripped.split("->", 1))
        rules.append(Rule(glob, tuple(d.strip() for d in docs.split(",") if d.strip())))
    return rules


def is_doc(path: str) -> bool:
    return any(_matches(path, glob) for glob in DOC_GLOBS)


def decide(*, changed_files: list[str], rules: list[Rule], data_stale: bool) -> Decision:
    if data_stale:
        return Decision(False, "generated docs data is stale", STALE_TEMPLATE)
    groups: dict[tuple[str, ...], list[str]] = {}
    for path in changed_files:
        for rule in rules:
            if _matches(path, rule.glob):
                groups.setdefault(rule.docs, []).append(path)
                break
    if not groups:
        return Decision(True, "no documented code changed")
    if any(is_doc(path) for path in changed_files):
        return Decision(True, "documentation changed with the code")
    lines = []
    for docs, paths in groups.items():
        more = f" (+{len(paths) - 4} more)" if len(paths) > 4 else ""
        lines.append(f"  {', '.join(sorted(paths)[:4])}{more}\n    -> review {', '.join(docs)}")
    message = BLOCK_TEMPLATE.format(groups="\n".join(lines))
    return Decision(False, "code changed without docs", message)
