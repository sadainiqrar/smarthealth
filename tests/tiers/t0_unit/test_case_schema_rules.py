import pytest
from pydantic import ValidationError

from tests.runner.schema import Case

pytestmark = pytest.mark.unit

BASE = {"id": "sys-010-rules", "title": "Rule fixture", "tier": "contract"}
API_STEP = {"api": {"method": "GET", "path": "/health", "expect": {"status": 200}}}
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
        Case.model_validate(
            {**BASE, "tier": "journey", "steps": [AI_STEP], "expect": {"api": {"status": 200}}}
        )
    assert "judge" in str(exc.value)


def test_ai_step_with_a_judge_is_accepted():
    case = Case.model_validate(
        {
            **BASE,
            "tier": "journey",
            "steps": [AI_STEP],
            "judge": JUDGE,
            "expect": {"api": {"status": 200}},
        }
    )
    assert case.judge.prompt.startswith("Did the assistant")


def test_judge_without_an_ai_step_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate({**BASE, "steps": [API_STEP], "judge": JUDGE})
    assert "judge" in str(exc.value)


def test_case_with_steps_but_no_assertion_is_rejected():
    """The hole: steps present, engine-supported, but nothing asserted."""
    with pytest.raises(ValidationError) as exc:
        Case.model_validate({**BASE, "steps": [{"api": {"method": "GET", "path": "/health"}}]})
    assert "asserts nothing" in str(exc.value)


def test_step_level_expect_satisfies_the_assertion_rule():
    case = Case.model_validate(
        {**BASE, "steps": [{"api": {"path": "/health", "expect": {"status": 200}}}]}
    )
    assert case.steps[0].api.expect.status == 200


def test_case_level_expect_satisfies_the_assertion_rule():
    case = Case.model_validate(
        {
            **BASE,
            "steps": [{"api": {"path": "/health"}}],
            "expect": {"api": {"status": 200}},
        }
    )
    assert case.expect.api.status == 200


def test_await_step_satisfies_the_assertion_rule():
    """A timeout-bounded wait is an assertion: it fails if the condition never holds."""
    case = Case.model_validate(
        {**BASE, "tier": "workflow", "steps": [{"await": {"workflow": "Book", "timeout": "30s"}}]}
    )
    assert case.steps[0].kind == "await"


def test_impl_backed_case_is_exempt_from_the_assertion_rule():
    case = Case.model_validate({**BASE, "impl": "tests/tiers/t4_journey/test_x.py::test_y"})
    assert case.impl


def test_blocked_case_is_exempt_from_the_assertion_rule():
    case = Case.model_validate(
        {**BASE, "status": "blocked", "blocked_on": "engine support lands in P4"}
    )
    assert case.status == "blocked"


def test_an_empty_case_level_expect_does_not_count_as_an_assertion():
    """`expect: {}` is syntactically valid and declares nothing."""
    with pytest.raises(ValidationError) as exc:
        Case.model_validate(
            {**BASE, "steps": [{"api": {"path": "/health"}}], "expect": {}}
        )
    assert "asserts nothing" in str(exc.value)


def test_an_empty_api_expect_does_not_count_as_an_assertion():
    """The exact shape a reviewer proved would execute a real request and check nothing."""
    with pytest.raises(ValidationError) as exc:
        Case.model_validate(
            {**BASE, "steps": [{"api": {"path": "/health"}}], "expect": {"api": {}}}
        )
    assert "asserts nothing" in str(exc.value)


def test_an_empty_per_step_expect_does_not_count_as_an_assertion():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate(
            {**BASE, "steps": [{"api": {"path": "/health", "expect": {}}}]}
        )
    assert "asserts nothing" in str(exc.value)


def test_an_empty_json_contains_does_not_count_as_an_assertion():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate(
            {
                **BASE,
                "steps": [{"api": {"path": "/health", "expect": {"json_contains": {}}}}],
            }
        )
    assert "asserts nothing" in str(exc.value)


def test_a_db_expectation_with_no_predicate_does_not_count():
    with pytest.raises(ValidationError) as exc:
        Case.model_validate(
            {
                **BASE,
                "tier": "integration",
                "steps": [{"api": {"path": "/health"}}],
                "expect": {"db": {"appointments": {}}},
            }
        )
    assert "asserts nothing" in str(exc.value)


def test_a_real_db_expectation_counts():
    case = Case.model_validate(
        {
            **BASE,
            "tier": "integration",
            "steps": [{"api": {"path": "/health"}}],
            "expect": {"db": {"appointments": {"count": 1}}},
        }
    )
    assert case.expect.db["appointments"].count == 1


def test_a_non_empty_events_expectation_counts():
    case = Case.model_validate(
        {
            **BASE,
            "tier": "integration",
            "steps": [{"api": {"path": "/health"}}],
            "expect": {"events": [{"topic": "appointments.booked", "count": 1}]},
        }
    )
    assert case.expect.events[0].topic == "appointments.booked"
