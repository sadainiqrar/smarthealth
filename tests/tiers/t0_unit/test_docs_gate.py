"""The docs Stop gate: blocks code-without-docs, allows everything else."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.runner.docs_gate import Rule, decide, is_doc, load_rules

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
RULES = [
    Rule("app/modules/**", ("docs/ARCHITECTURE.md", "docs/index.html")),
    Rule("docker-compose*.yml", ("README.md",)),
]


def test_nothing_documented_changed_allows():
    decision = decide(changed_files=["tests/cases/x.yaml", "notes.txt"], rules=RULES,
                      data_stale=False)
    assert decision.allow


def test_code_without_docs_blocks_and_names_the_docs():
    decision = decide(changed_files=["app/modules/scheduling/service.py"], rules=RULES,
                      data_stale=False)
    assert not decision.allow
    assert "app/modules/scheduling/service.py" in decision.message
    assert "docs/ARCHITECTURE.md" in decision.message
    assert "DOCS_WAIVE" in decision.message


def test_code_with_any_doc_allows():
    decision = decide(changed_files=["app/modules/scheduling/service.py", "docs/PRD.md"],
                      rules=RULES, data_stale=False)
    assert decision.allow


def test_stale_generated_data_blocks_even_with_docs_changed():
    decision = decide(changed_files=["docs/PRD.md"], rules=RULES, data_stale=True)
    assert not decision.allow
    assert "gen_docs_data" in decision.message


def test_many_files_are_summarised():
    files = [f"app/modules/m{i}.py" for i in range(7)]
    decision = decide(changed_files=files, rules=RULES, data_stale=False)
    assert "(+3 more)" in decision.message


@pytest.mark.parametrize("path, expected", [
    ("README.md", True), ("docs/PRD.md", True), ("docs/index.html", True),
    ("docs/data/schema.json", True), ("services/assistant/README.md", True),
    ("docs/superpowers/specs/x.md", False), ("app/main.py", False),
])
def test_what_counts_as_documentation(path, expected):
    assert is_doc(path) is expected


def test_the_real_map_parses_and_covers_the_application():
    rules = load_rules(ROOT / "docs" / "doc-map.txt")
    globs = {r.glob for r in rules}
    assert {"app/modules/**", "services/assistant/**", "services/knowledge/**",
            "docker-compose*.yml", "migrations/**"} <= globs
    assert all(r.docs for r in rules)
