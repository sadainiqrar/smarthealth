"""The Celery application: fire-and-forget jobs over RabbitMQ (PRD D-7; spec section 5).

Celery carries *commands* -- "send this message", "sweep stuck rows" -- to exactly one
worker. Facts go over Kafka, multi-step processes are Temporal's. Two queues, both named
through `Settings.queue()` (harness requirement 1):

- `notifications`: one task per message. Dead-lettered to `notifications.dlq` when its
  retries are exhausted, so a message that cannot be delivered is kept, not dropped.
- `maintenance`: the beat sweeps.

Reliability settings, each for a reason:

- `task_acks_late` + `task_reject_on_worker_lost`: a message is acknowledged after the task
  finishes, so a worker killed mid-task leaves it to be redelivered, not lost. Redelivery is
  safe because the send task claims its row first (app/modules/notifications/service.py).
- `worker_prefetch_multiplier = 1`: a worker holds one unacknowledged message at a time, so
  a slow SMTP server does not strand a batch of others behind it.
- `task_ignore_result`: nothing reads results; a result backend would be state to run for
  nobody.

Every task registers in `TASKS` (harness requirement 3); a meta-test fails on one that
does not, or that is not `acks_late`.

    celery -A app.celery_app worker -Q notifications,maintenance
    celery -A app.celery_app beat
"""

from __future__ import annotations

from datetime import timedelta

from celery import Celery, signals
from kombu import Exchange, Queue

from app.core.registry import Registry
from app.settings import Settings, get_settings

celery = Celery("smarthealth")

#: Every task, by name. Populated by the task modules at import, enumerated by the meta-test.
TASKS: Registry[object] = Registry("celery task")

TASK_MODULES = (
    "app.modules.notifications.tasks",
    "app.modules.scheduling.tasks",
    "app.events.maintenance",
    "app.modules.analytics.tasks",
    "app.modules.clinical.tasks",
)


def queues(settings: Settings) -> dict[str, Queue]:
    notifications = settings.queue("notifications")
    dead_letters = f"{notifications}.dlq"
    return {
        "notifications": Queue(
            notifications, Exchange(notifications, type="direct"), routing_key=notifications,
            queue_arguments={
                # The default exchange routes by queue name: a rejected message lands in
                # the dead-letter queue, untouched, with its x-death history.
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": dead_letters,
            },
        ),
        "dead_letters": Queue(dead_letters, Exchange(""), routing_key=dead_letters),
        "maintenance": Queue(
            settings.queue("maintenance"), Exchange(settings.queue("maintenance"), type="direct"),
            routing_key=settings.queue("maintenance"),
        ),
        # Text extraction from uploaded reports (Week 4 slice 2).
        "documents": Queue(
            settings.queue("documents"), Exchange(settings.queue("documents"), type="direct"),
            routing_key=settings.queue("documents"),
        ),
    }


_configured: dict[str, Settings] = {}


def task_settings() -> Settings:
    """The settings the app was configured with -- what every task reads, so a test that
    configures the app for its own stack (a prefix, offset ports) gets tasks that use it."""
    return _configured.get("settings") or get_settings()


def configure(settings: Settings) -> Celery:
    _configured["settings"] = settings
    named = queues(settings)
    celery.conf.update(
        broker_url=settings.rabbitmq_url,
        broker_connection_retry_on_startup=True,
        task_queues=[named["notifications"], named["maintenance"], named["documents"]],
        task_default_queue=named["notifications"].name,
        task_routes={
            "notifications.send": {"queue": named["notifications"].name},
            "notifications.requeue_stuck": {"queue": named["maintenance"].name},
            "scheduling.reconcile_stale_pending": {"queue": named["maintenance"].name},
            "events.purge_published": {"queue": named["maintenance"].name},
            "analytics.reconcile": {"queue": named["maintenance"].name},
            "clinical.extract_document": {"queue": named["documents"].name},
        },
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_ignore_result=True,
        task_serializer="json",
        accept_content=["json"],
        beat_schedule={
            "requeue-stuck-notifications": {
                "task": "notifications.requeue_stuck", "schedule": timedelta(minutes=1),
            },
            "reconcile-stale-pending": {
                "task": "scheduling.reconcile_stale_pending", "schedule": timedelta(minutes=5),
            },
            "purge-published-outbox": {
                "task": "events.purge_published", "schedule": timedelta(hours=24),
            },
            "reconcile-analytics": {
                "task": "analytics.reconcile", "schedule": timedelta(hours=24),
            },
        },
        include=list(TASK_MODULES),
    )
    return celery


def enqueue_send(notification_id: str) -> None:
    """Hand one notification to the workers. By name, so callers need not import the task
    module (and the Celery app stays a leaf the domain code can reach)."""
    celery.send_task(
        "notifications.send", args=[notification_id], queue=celery.conf.task_default_queue
    )


@signals.worker_ready.connect
def _declare_dead_letter_queue(sender=None, **_kwargs) -> None:
    """The worker does not consume the dead-letter queue, so nothing would declare it;
    declared here, or RabbitMQ drops dead letters routed to a queue that does not exist."""
    with celery.connection_for_write() as connection:
        queues(task_settings())["dead_letters"].bind(connection.default_channel).declare()


@signals.worker_process_init.connect
def _telemetry(**_kwargs) -> None:
    from app.core.telemetry import setup_telemetry

    setup_telemetry(task_settings(), "part-a-celery")


configure(get_settings())
