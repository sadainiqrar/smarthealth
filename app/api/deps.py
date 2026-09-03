"""HTTP-layer dependency providers.

This is the seam between the framework and the framework-free core: functions here
take a FastAPI `Request` and hand back a plain object the rest of the application
already knows how to use. It lives in `app.api`, not `app.core`, so that code which
must stay importable without FastAPI — `app.core.audit`, and Week 2's Temporal
activities that call the same service functions — never needs to import this module or
anything that transitively pulls in FastAPI or Starlette.

Keeping `get_audit_log` here, rather than next to `AuditLog` in `app.core.audit`, is
what lets a Temporal activity construct an `AuditLog` directly from a Mongo collection
and a clock, with no ASGI stack involved at all.
"""

from __future__ import annotations

from fastapi import Request

from app.core.audit import AuditLog
from app.core.clock import get_clock
from app.db.mongo import get_audit_collection


def get_audit_log(request: Request) -> AuditLog:
    """FastAPI dependency. Services receive an `AuditLog`, never the request."""
    settings = request.app.state.settings
    collection = get_audit_collection(request.app.state.mongo, settings)
    return AuditLog(collection, get_clock())
