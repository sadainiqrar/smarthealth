import textwrap

import pytest

from tests.runner.route_check import main

pytestmark = pytest.mark.unit

VALID = """
    id: sys-030-valid
    title: A valid case
    tier: contract
    priority: P0
    requirement: [PART-A-OBS-1]
    steps: [{ api: { path: /health, expect: { status: 200 } } }]
"""

INVALID = """
    id: sys-031-invalid
    title: Asserts nothing
    tier: contract
"""


def write(tmp_path, name, body):
    (tmp_path / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_exit_zero_when_every_case_is_valid(tmp_path, capsys):
    write(tmp_path, "sys-030-valid.yaml", VALID)
    assert main(["--cases-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "sys-030-valid" in out
    assert "1 case" in out


def test_exit_one_when_a_case_is_invalid(tmp_path, capsys):
    write(tmp_path, "sys-031-invalid.yaml", INVALID)
    assert main(["--cases-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "anti-stub" in out


def test_empty_catalog_is_valid(tmp_path):
    assert main(["--cases-dir", str(tmp_path)]) == 0


def test_write_catalog_creates_the_file(tmp_path):
    write(tmp_path, "sys-030-valid.yaml", VALID)
    assert main(["--cases-dir", str(tmp_path), "--write-catalog"]) == 0
    catalog = (tmp_path / "CATALOG.md").read_text(encoding="utf-8")
    assert "sys-030-valid" in catalog


def test_write_traceability_creates_the_file(tmp_path):
    write(tmp_path, "sys-030-valid.yaml", VALID)
    reports = tmp_path / "reports"
    assert main([
        "--cases-dir", str(tmp_path),
        "--write-traceability", str(reports / "traceability.md"),
    ]) == 0
    assert "PART-A-OBS-1" in (reports / "traceability.md").read_text(encoding="utf-8")


def test_reports_are_not_written_when_validation_fails(tmp_path):
    write(tmp_path, "sys-031-invalid.yaml", INVALID)
    assert main(["--cases-dir", str(tmp_path), "--write-catalog"]) == 1
    assert not (tmp_path / "CATALOG.md").exists()
