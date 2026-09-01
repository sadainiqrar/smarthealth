import textwrap

import pytest

from tests.runner.schema import Case, CaseValidationError

pytestmark = pytest.mark.unit

MINIMAL = {
    "id": "sys-001-example",
    "title": "An example case",
    "tier": "contract",
    "steps": [{"api": {"method": "GET", "path": "/health"}}],
}


def write_case(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_minimal_case_validates():
    case = Case.model_validate(MINIMAL)
    assert case.id == "sys-001-example"
    assert case.tier == "contract"
    assert case.priority == "P1"
    assert case.status == "ready"
    assert case.requirement == []


def test_id_must_be_kebab_case():
    with pytest.raises(Exception) as exc:
        Case.model_validate({**MINIMAL, "id": "Sys_001_Example"})
    assert "kebab-case" in str(exc.value)


def test_requirement_accepts_a_bare_string():
    case = Case.model_validate({**MINIMAL, "requirement": "PART-A-FR-2"})
    assert case.requirement == ["PART-A-FR-2"]


def test_unknown_field_is_rejected():
    with pytest.raises(Exception):
        Case.model_validate({**MINIMAL, "nonsense": True})


def test_from_file_requires_id_to_match_filename(tmp_path):
    path = write_case(
        tmp_path,
        "sys-002-mismatched.yaml",
        """
        id: sys-001-example
        title: An example case
        tier: contract
        steps:
          - api: { method: GET, path: /health }
        """,
    )
    with pytest.raises(CaseValidationError) as exc:
        Case.from_file(path)
    assert "filename stem" in str(exc.value)


def test_from_file_reports_invalid_yaml(tmp_path):
    path = write_case(tmp_path, "sys-003-broken.yaml", "id: [unclosed\n")
    with pytest.raises(CaseValidationError) as exc:
        Case.from_file(path)
    assert "invalid YAML" in str(exc.value)


def test_from_file_reports_a_non_mapping(tmp_path):
    path = write_case(tmp_path, "sys-004-list.yaml", "- not\n- a mapping\n")
    with pytest.raises(CaseValidationError) as exc:
        Case.from_file(path)
    assert "mapping" in str(exc.value)


def test_from_file_loads_a_valid_case(tmp_path):
    path = write_case(
        tmp_path,
        "sys-005-valid.yaml",
        """
        id: sys-005-valid
        title: A valid case
        requirement: [PART-A-FR-1]
        tier: contract
        priority: P0
        steps:
          - api:
              method: GET
              path: /health
              expect: { status: 200 }
        """,
    )
    case = Case.from_file(path)
    assert case.priority == "P0"
    assert case.steps[0].api.path == "/health"
    assert case.steps[0].api.expect.status == 200
