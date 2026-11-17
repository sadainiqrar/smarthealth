"""Booking outcomes as a metric (system health spec §4.1).

`BookAppointmentWorkflow` completes normally when a booking compensates -- a declined
payment or a lost slot ends the appointment `failed`, not the workflow -- so Temporal's
workflow counters cannot say how many bookings failed. This counter can: it is recorded
once per booking that reaches a final outcome, by whatever wrote that outcome (the
`booking.confirm` and `booking.fail_booking` activities, and the stale-pending reconciler),
after the transaction committed. Prometheus sees
`smarthealth_bookings_total{outcome="confirmed"|"failed"}`.
"""

from __future__ import annotations

from opentelemetry import metrics

_bookings = metrics.get_meter("smarthealth.scheduling").create_counter(
    "smarthealth.bookings", unit="1",
    description="Bookings that reached a final outcome (confirmed, failed)",
)


def booking_confirmed() -> None:
    _bookings.add(1, {"outcome": "confirmed"})


def booking_failed() -> None:
    _bookings.add(1, {"outcome": "failed"})
