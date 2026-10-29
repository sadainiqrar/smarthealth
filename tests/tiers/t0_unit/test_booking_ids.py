import uuid

import pytest

from app.modules.scheduling.booking import (
    appointment_id_for,
    booking_workflow_id,
    reschedule_appointment_id_for,
)

pytestmark = pytest.mark.unit


def test_the_same_caller_and_key_always_name_the_same_appointment():
    assert appointment_id_for("user-1", "k") == appointment_id_for("user-1", "k")


def test_different_callers_cannot_collide_on_one_key():
    assert appointment_id_for("user-1", "k") != appointment_id_for("user-2", "k")


def test_no_key_means_a_new_booking_every_time():
    assert appointment_id_for("user-1", None) != appointment_id_for("user-1", None)


def test_a_keyless_id_is_time_ordered_like_every_other_key():
    assert appointment_id_for("user-1", None).version == 7


def test_a_keyed_id_is_a_uuid5_because_it_must_be_derivable():
    assert appointment_id_for("user-1", "k").version == 5


def test_the_workflow_id_is_derived_from_the_appointment():
    appointment_id = uuid.uuid4()
    assert booking_workflow_id(appointment_id) == f"booking-{appointment_id}"


def test_a_key_used_for_a_booking_and_a_reschedule_names_two_appointments():
    """M-3: one namespace would make them one id, and the reschedule's child booking would
    land on the booking's `booking-<id>`."""
    assert reschedule_appointment_id_for("user-1", "k") != appointment_id_for("user-1", "k")


def test_a_reschedule_id_is_derivable_and_per_caller():
    assert reschedule_appointment_id_for("user-1", "k") == reschedule_appointment_id_for(
        "user-1", "k"
    )
    assert reschedule_appointment_id_for("user-1", "k") != reschedule_appointment_id_for(
        "user-2", "k"
    )
    assert reschedule_appointment_id_for("user-1", "k").version == 5


def test_a_keyless_reschedule_id_is_new_and_time_ordered():
    first = reschedule_appointment_id_for("user-1", None)
    assert first != reschedule_appointment_id_for("user-1", None)
    assert first.version == 7
