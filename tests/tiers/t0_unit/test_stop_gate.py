import pytest

from tests.runner.stop_gate import decide, load_globs, matched_core_paths, parse_porcelain

pytestmark = pytest.mark.unit


def test_parse_porcelain_reads_nul_delimited_entries():
    raw = "M  app/main.py\x00?? tests/cases/new.yaml\x00"
    assert parse_porcelain(raw) == ["app/main.py", "tests/cases/new.yaml"]


def test_parse_porcelain_takes_the_new_path_of_a_rename():
    raw = "R  app/new.py\x00app/old.py\x00 M app/other.py\x00"
    assert parse_porcelain(raw) == ["app/new.py", "app/other.py"]


def test_parse_porcelain_handles_empty_input():
    assert parse_porcelain("") == []


def test_load_globs_skips_comments_and_blanks(tmp_path):
    path = tmp_path / "core-paths.txt"
    path.write_text("# a comment\n\n  app/**  \nservices/**\n", encoding="utf-8")
    assert load_globs(path) == ["app/**", "services/**"]


def test_load_globs_returns_empty_for_a_missing_file(tmp_path):
    assert load_globs(tmp_path / "absent.txt") == []


def test_matched_core_paths_matches_nested_files():
    changed = ["app/api/routes/appointments.py", "docs/readme.md"]
    assert matched_core_paths(changed, ["app/**"]) == ["app/api/routes/appointments.py"]


def test_matched_core_paths_matches_a_top_level_file():
    assert matched_core_paths(["app/main.py"], ["app/**"]) == ["app/main.py"]


def test_allows_when_no_core_path_changed():
    decision = decide(changed_files=["README.md"], core_globs=["app/**"],
                      has_changed_case=False, route_check_ok=False)
    assert decision.allow
    assert decision.reason == "no core path changed"


def test_allows_when_no_globs_are_configured():
    decision = decide(changed_files=["app/main.py"], core_globs=[],
                      has_changed_case=False, route_check_ok=False)
    assert decision.allow
    assert decision.reason == "no core paths configured"


def test_allows_on_a_waiver_and_records_it():
    decision = decide(changed_files=["app/main.py"], core_globs=["app/**"],
                      has_changed_case=False, route_check_ok=False,
                      waiver="spike, throwaway branch")
    assert decision.allow
    assert decision.waiver == "spike, throwaway branch"


def test_blocks_when_core_code_changed_with_no_case():
    decision = decide(changed_files=["app/main.py"], core_globs=["app/**"],
                      has_changed_case=False, route_check_ok=True)
    assert not decision.allow
    assert "app/main.py" in decision.message
    assert "/feature-test" in decision.message
    assert "E2E_WAIVE" in decision.message


def test_blocks_when_the_case_exists_but_is_invalid():
    decision = decide(changed_files=["app/main.py"], core_globs=["app/**"],
                      has_changed_case=True, route_check_ok=False)
    assert not decision.allow
    assert "did not validate" in decision.message


def test_allows_when_a_valid_case_accompanies_the_change():
    decision = decide(changed_files=["app/main.py", "tests/cases/apt-001-x.yaml"],
                      core_globs=["app/**"], has_changed_case=True, route_check_ok=True)
    assert decision.allow
    assert decision.reason == "covered by a validated case"


def test_block_messages_are_pure_ascii():
    """The hook writes these to stderr for Claude Code to render.

    A U+2026 ellipsis encodes to cp1252 byte 0x85, which is not valid UTF-8, so a
    non-ASCII character here silently corrupts the one message that must be readable
    when the gate fires.
    """
    from tests.runner.stop_gate import BLOCK_TEMPLATE, INVALID_CASE_TEMPLATE

    for template in (BLOCK_TEMPLATE, INVALID_CASE_TEMPLATE):
        offenders = [character for character in template if ord(character) > 127]
        assert not offenders, f"non-ASCII in a block message: {offenders}"

    truncated = decide(
        changed_files=[f"app/mod{index}.py" for index in range(7)],
        core_globs=["app/**"],
        has_changed_case=False,
        route_check_ok=True,
    )
    offenders = [character for character in truncated.message if ord(character) > 127]
    assert not offenders, f"non-ASCII in the truncated message: {offenders}"
    assert truncated.message.count("app/mod") == 5
    assert len(truncated.matched) == 7
