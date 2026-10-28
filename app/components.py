"""The one place Temporal components are registered.

Not in the workflow modules themselves: Temporal re-imports a workflow's module inside
its sandbox, so a registration call there would run twice. This module is imported by
the worker and the tests, never by the sandbox.

Each slice that adds a workflow or activity adds a line here.
tests/meta/test_temporal_registry.py fails if a definition in the tree is missing.
"""

from __future__ import annotations

from app.core.temporal import WORKFLOWS
from app.core.temporal_canary import CanaryWorkflow

_registered = False


def register_all() -> None:
    """Populate the registries. Idempotent."""
    global _registered
    if _registered:
        return
    WORKFLOWS.register(CanaryWorkflow.__name__, CanaryWorkflow)
    _registered = True
