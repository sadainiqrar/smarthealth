"""What other modules may use from billing (the D-15 pattern).

Scheduling's activities call these to authorise a booking, release its money on
compensation, cancellation or reschedule, and capture it after the visit. Each is a
declared edge in `CROSS_MODULE_ALLOWED` (tests/meta/test_import_boundaries.py);
`scheduling/service.py` itself imports nothing from billing.
"""

from __future__ import annotations

from app.modules.billing.gateway import GatewayRejected, GatewayUnavailable, PaymentGateway
from app.modules.billing.models import BillingRecord, BillingStatus
from app.modules.billing.service import authorize, capture, release

__all__ = [
    "BillingRecord",
    "BillingStatus",
    "GatewayRejected",
    "GatewayUnavailable",
    "PaymentGateway",
    "authorize",
    "capture",
    "release",
]
