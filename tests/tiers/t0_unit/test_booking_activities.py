"""Activity input parsing: a malformed id fails once, permanently."""

import uuid
from datetime import timedelta

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from app.components import build_activities
from app.modules.scheduling.activities import SchedulingActivities, parse_id
from app.modules.scheduling.contracts import BookingRequest, FollowUpRequest, RescheduleRequest

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


# --- slice 4 activities ----------------------------------------------------------------

BAD = "not-a-uuid"
GOOD = str(uuid.uuid4())


def _activities() -> SchedulingActivities:
    """No resources: a malformed id must fail before any is touched."""
    return SchedulingActivities(None, None, None, timedelta(minutes=1))


@pytest.mark.parametrize(
    ("call", "field"),
    [
        (lambda a: a.create_pending(BookingRequest(GOOD, GOOD, GOOD, None, None, BAD)),
         "rescheduled_from_id"),
        (lambda a: a.reschedule_complete(RescheduleRequest(BAD, GOOD, GOOD, GOOD, None)),
         "old_appointment_id"),
        (lambda a: a.waitlist_candidates(BAD), "slot_id"),
        (lambda a: a.waitlist_fulfil(BAD), "entry_id"),
    ],
    ids=["create_pending", "reschedule_complete", "waitlist_candidates", "waitlist_fulfil"],
)
async def test_the_new_activities_fail_a_malformed_id_once_and_permanently(call, field):
    with pytest.raises(ApplicationError) as caught:
        await call(_activities())
    assert caught.value.non_retryable and field in caught.value.message


async def test_the_refund_stand_in_needs_no_database():
    await _activities().refund(FollowUpRequest(GOOD, None, None))


def test_a_booking_request_recorded_before_the_reschedule_field_still_decodes():
    """The replay-safety half that the recorded history cannot show for a new field on a
    *different* request shape: JSON without the key decodes to the default."""
    payload = (
        b'{"appointment_id": "a", "patient_id": "p", "slot_id": "s", '
        b'"actor_user_id": null, "reason": null}'
    )
    decoded = DataConverter.default.payload_converter.from_payloads(
        [Payload(metadata={"encoding": b"json/plain"}, data=payload)], [BookingRequest]
    )
    assert decoded[0].rescheduled_from_id is None
