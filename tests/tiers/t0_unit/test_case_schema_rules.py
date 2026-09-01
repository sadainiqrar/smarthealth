import pytest
from pydantic import ValidationError

from tests.runner.schema import Case

pytestmark = pytest.mark.unit

BASE = {"id": "sys-010-rules", "title": "Rule fixture", "tier": "contract"}
API_STEP = {"api": {"method": "GET", "path": "/health"}}
AI_STEP = {"ai": {"ask": "Which specialist should I see for chest pain?"}}
JUDGE = {"prompt": "Did the assistant decline to diagnose and route to a provider?"}


def test_case_with_neither_steps_nor_impl_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate(BASE)
    assert "anti-stub" in str(exc.value)


def test_impl_only_case_is_accepted():
    case = Case.model_validate({**BASE, "impl": "tests/tiers/t4_journey/test_x.py::test_y"})
    assert case.steps == []
    assert case.impl.endswith("::test_y")


def test_blocked_case_needs_no_steps_but_needs_a_reason():
    case = Case.model_validate(
        {**BASE, "status": "blocked", "blocked_on": "engine support for chaos lands in P4"}
    )
    assert case.status == "blocked"


def test_blocked_without_a_reason_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate({**BASE, "status": "blocked"})
    assert "blocked_on" in str(exc.value)


def test_reason_without_blocked_status_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate({**BASE, "steps": [API_STEP], "blocked_on": "why?"})
    assert "blocked_on" in str(exc.value)


def test_ai_step_requires_a_judge():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate({**BASE, "tier": "journey", "steps": [AI_STEP]})
    assert "judge" in str(exc.value)


def test_ai_step_with_a_judge_is_accepted():
    case = Case.model_validate(
        {**BASE, "tier": "journey", "steps": [AI_STEP], "judge": JUDGE}
    )
    assert case.judge.prompt.startswith("Did the assistant")


def test_judge_without_an_ai_step_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate({**BASE, "steps": [API_STEP], "judge": JUDGE})
    assert "judge" in str(exc.value)
