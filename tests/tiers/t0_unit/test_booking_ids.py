import uuid

import pytest

from app.modules.scheduling.booking import appointment_id_for, booking_workflow_id

pytestmark = pytest.mark.unit


def test_the_same_caller_and_key_always_name_the_same_appointment():
    assert appointment_id_for("user-1", "k") == appointment_id_for("user-1", "k")


def test_different_callers_cannot_collide_on_one_key():
    assert appointment_id_for("user-1", "k") != appointment_id_for("user-2", "k")


def test_no_key_means_a_new_booking_every_time():
    assert appointment_id_for("user-1", None) != appointment_id_for("user-1", None)


def test_the_workflow_id_is_derived_from_the_appointment():
    appointment_id = uuid.uuid4()
    assert booking_workflow_id(appointment_id) == f"booking-{appointment_id}"
