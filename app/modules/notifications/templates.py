"""What each kind of message says. Plain text, rendered from a small context.

Messages carry no clinical content (spec D-31): appointment times and next steps only. A
missing context key renders as an empty string rather than failing the send -- a reminder
with no time in it is better than no reminder.
"""

from __future__ import annotations

from datetime import datetime
from string import Formatter
from typing import Any

TEMPLATES: dict[str, tuple[str, str]] = {
    "booking_confirmed": (
        "Your appointment is confirmed",
        "Hello {name},\n\nYour appointment on {when} is confirmed.\n\n"
        "To change it, use the patient portal or call the front desk.\n\nMediNova",
    ),
    "appointment_cancelled": (
        "Your appointment has been cancelled",
        "Hello {name},\n\nYour appointment on {when} has been cancelled.{why}\n\n"
        "Any hold on your card has been released.\n\nMediNova",
    ),
    "appointment_rescheduled": (
        "Your appointment has moved",
        "Hello {name},\n\nYour appointment is now on {when}.\n\nMediNova",
    ),
    "appointment_missed": (
        "We missed you today",
        "Hello {name},\n\nWe did not see you for your appointment on {when}. "
        "Please book a new time when it suits you.\n\nMediNova",
    ),
    "appointment_reminder": (
        "Reminder: your appointment",
        "Hello {name},\n\nA reminder of your appointment on {when}.\n\n"
        "If you cannot come, please cancel so someone else can have the time.\n\nMediNova",
    ),
    "refund_issued": (
        "Your refund is on its way",
        "Hello {name},\n\nThe charge for your appointment has been refunded.\n\nMediNova",
    ),
}

#: The reason a cancellation gives the patient, by the cancellation's `reason`.
CANCELLATION_REASONS = {
    "provider_unavailable": " Your clinician is no longer available; please book with "
                            "another clinician -- the waitlist can find you a time.",
}


class _Lenient(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _when(value: Any) -> str:
    if isinstance(value, str) and value:
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value.strftime("%A %d %B %Y at %H:%M UTC")
    return "your booked time"


def render(kind: str, context: dict[str, Any]) -> tuple[str, str]:
    subject, body = TEMPLATES[kind]
    values = _Lenient(context)
    values["when"] = _when(context.get("when"))
    values["why"] = CANCELLATION_REASONS.get(context.get("reason") or "", "")
    return subject, Formatter().vformat(body, (), values)
