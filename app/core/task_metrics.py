"""Celery task outcomes as a metric (system health spec §3).

Framework-free on purpose (the handlers take Celery's signal keyword arguments, not Celery
types), so `app/core` stays importable without a broker. Connected in `app/celery_app.py`.
Prometheus sees `smarthealth_celery_tasks_total{task, outcome}`; `task` is the registered
task name, a small fixed set.
"""

from __future__ import annotations

from opentelemetry import metrics

_tasks = metrics.get_meter("smarthealth.celery").create_counter(
    "smarthealth.celery.tasks", unit="1",
    description="Celery task runs by task name and outcome (success, retry, failure)",
)


def _name(sender) -> str:
    return getattr(sender, "name", None) or "unknown"


def on_success(sender=None, **_kwargs) -> None:
    _tasks.add(1, {"task": _name(sender), "outcome": "success"})


def on_retry(sender=None, request=None, **_kwargs) -> None:
    # Prefer the request's task name; fall back to the sender.
    name = getattr(request, "task", None) or _name(sender)
    _tasks.add(1, {"task": name, "outcome": "retry"})


def on_failure(sender=None, **_kwargs) -> None:
    _tasks.add(1, {"task": _name(sender), "outcome": "failure"})


def on_postrun(sender=None, state=None, **_kwargs) -> None:
    """A run that ended in `Reject` (a dead-lettered notification) gets no task_failure
    signal from Celery, only task_postrun with state REJECTED -- count it as a failure.
    Every other state is already counted by the handlers above."""
    if state == "REJECTED":
        _tasks.add(1, {"task": _name(sender), "outcome": "failure"})
