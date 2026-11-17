"""The health page's flow list names things that exist.

A Temporal metric's `workflow_type` label is the name in `@workflow.defn(name=...)`, not the
class name, and a Celery series carries the task's registered name. A flow written with the
wrong spelling queries a series that never exists and shows "idle" for ever, so this test
checks every name against the registries.
"""

import importlib

import pytest
from temporalio.workflow import _Definition

from app.celery_app import TASK_MODULES, TASKS
from app.components import register_all
from app.core.temporal import WORKFLOWS
from app.modules.monitoring.queries import FLOWS

pytestmark = pytest.mark.meta

register_all()
for _module in TASK_MODULES:
    importlib.import_module(_module)

WORKFLOW_TYPES = {_Definition.must_from_class(cls).name for cls in WORKFLOWS}
TASK_NAMES = set(TASKS.names())


@pytest.mark.parametrize("flow", [f for f in FLOWS if f.kind == "workflow"], ids=lambda f: f.key)
def test_a_workflow_flow_uses_the_registered_workflow_type(flow):
    assert flow.name in WORKFLOW_TYPES, (
        f"{flow.name} is not a registered workflow type; registered: {sorted(WORKFLOW_TYPES)}")


@pytest.mark.parametrize("flow", [f for f in FLOWS if f.kind == "task"], ids=lambda f: f.key)
def test_a_task_flow_uses_a_registered_celery_task(flow):
    assert flow.name in TASK_NAMES, f"{flow.name} is not in app.celery_app.TASKS"
