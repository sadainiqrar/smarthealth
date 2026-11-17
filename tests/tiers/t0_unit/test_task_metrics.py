"""Celery task outcomes become a metric, so notifications, reminders and document
extraction are visible on the health page (spec §3)."""

import pytest

from app.core import task_metrics

pytestmark = pytest.mark.unit


class _Task:
    name = "notifications.send"


def _count(metric_reader, task: str, outcome: str) -> float:
    data = metric_reader.get_metrics_data()
    total = 0.0
    if data is None:  # nothing recorded yet
        return total
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != "smarthealth.celery.tasks":
                    continue
                for point in metric.data.data_points:
                    if point.attributes == {"task": task, "outcome": outcome}:
                        total += point.value
    return total


@pytest.mark.parametrize(
    ("handler", "outcome"),
    [
        (task_metrics.on_success, "success"),
        (task_metrics.on_retry, "retry"),
        (task_metrics.on_failure, "failure"),
    ],
)
def test_each_signal_counts_its_outcome_by_task_name(metric_reader, handler, outcome):
    before = _count(metric_reader, "notifications.send", outcome)
    if handler is task_metrics.on_retry:
        handler(sender=None, request=type("R", (), {"task": "notifications.send"})())
    else:
        handler(sender=_Task())
    assert _count(metric_reader, "notifications.send", outcome) == before + 1


def test_an_unknown_sender_is_counted_as_unknown(metric_reader):
    before = _count(metric_reader, "unknown", "failure")
    task_metrics.on_failure(sender=None)
    assert _count(metric_reader, "unknown", "failure") == before + 1


def test_the_handlers_are_connected_to_celerys_signals():
    from celery import signals

    import app.celery_app  # noqa: F401 - connects the handlers on import

    for signal, handler in [
        (signals.task_success, task_metrics.on_success),
        (signals.task_retry, task_metrics.on_retry),
        (signals.task_failure, task_metrics.on_failure),
    ]:
        assert any(receiver is handler for receiver in signal._live_receivers(None)), handler
