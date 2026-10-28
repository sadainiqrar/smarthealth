"""Activity input parsing: a malformed id fails once, permanently."""

import uuid

import pytest
from temporalio.exceptions import ApplicationError

from app.modules.scheduling.activities import parse_id

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
