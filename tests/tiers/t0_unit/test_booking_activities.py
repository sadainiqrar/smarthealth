"""Activity input parsing: a malformed id fails once, permanently."""

import uuid
from datetime import timedelta

import pytest
from temporalio.exceptions import ApplicationError

from app.components import build_activities
from app.modules.scheduling.activities import SchedulingActivities, parse_id

pytestmark = pytest.mark.unit


def test_a_valid_id_parses():
    value = uuid.uuid4()
    assert parse_id(str(value), "slot_id") == value


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "1234"])
def test_a_malformed_id_is_a_non_retryable_invalid_request(bad):
    """Retrying can never make it parse; a retryable error would burn every attempt
    and, under compensation's unlimited policy, never stop."""
    with pytest.raises(ApplicationError) as caught:
        parse_id(bad, "slot_id")
    assert caught.value.non_retryable
    assert caught.value.type == "InvalidRequest"
    assert "slot_id" in caught.value.message


def test_the_no_show_grace_has_no_second_default():
    """`Settings.no_show_grace_minutes` is the one default; a construction site that
    forgets the grace must fail, not quietly use 15 minutes."""
    with pytest.raises(TypeError):
        SchedulingActivities(None, None, None)


def test_build_activities_hands_the_grace_to_the_visit_activities():
    bound = build_activities(None, None, None, timedelta(minutes=3))
    owners = {fn.__self__ for fn in bound if hasattr(fn, "__self__")}
    assert [o._no_show_grace for o in owners] == [timedelta(minutes=3)]
