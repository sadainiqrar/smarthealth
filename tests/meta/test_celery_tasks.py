"""Every Celery task is registered and safe to redeliver (harness requirement 3).

Registry-driven: the task modules are imported, every task Celery knows (bar its own
`celery.*` built-ins) must be in `TASKS`, and every one must acknowledge late -- a task
that acknowledges on receipt is lost when its worker dies mid-run, which is the failure
`acks_late` + `task_reject_on_worker_lost` exist to prevent.
"""

import importlib

import pytest

from app.celery_app import TASK_MODULES, TASKS, celery

pytestmark = pytest.mark.meta

for module in TASK_MODULES:
    importlib.import_module(module)

OURS = sorted(name for name in celery.tasks if not name.startswith("celery."))


def test_there_are_tasks_to_check():
    assert len(OURS) >= 4


@pytest.mark.parametrize("name", OURS)
def test_every_task_is_registered(name):
    assert name in TASKS, f"{name} is a Celery task missing from TASKS (app/celery_app.py)"


@pytest.mark.parametrize("name", OURS)
def test_every_task_acknowledges_late(name):
    assert celery.tasks[name].acks_late, f"{name} must be acks_late"


@pytest.mark.parametrize("name", OURS)
def test_every_task_is_routed_to_a_named_queue(name):
    assert name in celery.conf.task_routes, f"{name} has no route in app/celery_app.py"
