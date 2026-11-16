"""Generate the documentation site's data from the code itself.

    python scripts/gen_docs_data.py          write docs/data/*.json
    python scripts/gen_docs_data.py --check  exit 1 if any file is out of date

The site (docs/index.html) renders three things it must never get wrong, so they are not
written by hand:

- `openapi.json` -- Part A's HTTP API, exactly as FastAPI publishes it at /openapi.json;
- `schema.json`  -- every table: Part A's from SQLAlchemy metadata, the knowledge service's
  from its own metadata, the assistant's from its `create table` statements;
- `catalog.json` -- Kafka topics (who emits, who consumes), Celery tasks and schedules,
  Temporal workflows and activities, from the registries the meta-tests already enumerate.

Output is deterministic (sorted, no timestamps), so `tests/meta/test_docs_data.py` can
regenerate it and fail when the committed copy has drifted from the code.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "data"
ASSISTANT_SCHEMA_MODULES = ("conversations.py", "usage.py", "reports.py")


def _ensure_paths() -> None:
    for path in (ROOT, ROOT / "services" / "knowledge", ROOT / "services" / "assistant"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


# -- schema ------------------------------------------------------------------------------


def _first_line(doc: str | None) -> str:
    return (doc or "").strip().split("\n\n")[0].replace("\n", " ").strip()


def _table(table, doc: str = "") -> dict:
    columns = []
    for column in table.columns:
        fks = [f"{fk.column.table.name}.{fk.column.name}" for fk in column.foreign_keys]
        columns.append({
            "name": column.name,
            "type": str(column.type),
            "nullable": bool(column.nullable),
            "primary_key": bool(column.primary_key),
            "references": sorted(fks),
            "unique": bool(column.unique),
        })
    constraints = sorted(
        c.name for c in table.constraints
        if c.name and type(c).__name__ in ("CheckConstraint", "UniqueConstraint",
                                           "ForeignKeyConstraint", "ExcludeConstraint")
    )
    indexes = sorted(i.name for i in table.indexes if i.name)
    return {"name": table.name, "doc": doc, "columns": columns,
            "constraints": constraints, "indexes": indexes}


def part_a_tables() -> list[dict]:
    from app.db import all_models  # noqa: F401 - registers every table
    from app.db.base import Base

    docs = {m.class_.__table__.name: _first_line(m.class_.__doc__)
            for m in Base.registry.mappers if hasattr(m.class_, "__table__")}
    return [_table(t, docs.get(t.name, "")) for t in sorted(Base.metadata.sorted_tables,
                                                            key=lambda t: t.name)]


def knowledge_tables() -> list[dict]:
    from knowledge_service import store

    return [_table(t) for t in sorted(store.metadata.sorted_tables, key=lambda t: t.name)]


_CREATE = re.compile(r"create table if not exists (\w+) \((.*?)\n\);", re.S | re.I)


def assistant_tables() -> list[dict]:
    """The assistant creates its tables with plain SQL at start-up; parse those statements."""
    tables = []
    package = ROOT / "services" / "assistant" / "assistant_service"
    for module in ASSISTANT_SCHEMA_MODULES:
        source = (package / module).read_text(encoding="utf-8")
        for name, body in _CREATE.findall(source):
            columns, constraints = [], []
            for raw in body.split("\n"):
                line = raw.strip().rstrip(",")
                if not line:
                    continue
                if line.lower().startswith(("primary key", "unique", "constraint", "foreign key")):
                    constraints.append(line)
                    continue
                parts = line.split()
                columns.append({
                    "name": parts[0], "type": " ".join(parts[1:]).replace(" not null", "")
                    .replace(" primary key", ""),
                    "nullable": not re.search(r"not null|primary key", line, re.I),
                    "primary_key": "primary key" in line.lower(), "references": [],
                    "unique": False,
                })
            pk = next((c for c in constraints if c.lower().startswith("primary key")), "")
            for column in columns:
                if re.search(rf"\b{column['name']}\b", pk):
                    column["primary_key"] = True
                    column["nullable"] = False
            tables.append({"name": name, "doc": f"assistant_service/{module}", "columns": columns,
                           "constraints": constraints, "indexes": []})
    return sorted(tables, key=lambda t: t["name"])


def schema() -> dict:
    return {
        "databases": [
            {"name": "postgres (Part A)", "owner": "app, worker, relay, consumers, celery",
             "migrations": "migrations/versions (Alembic)", "tables": part_a_tables()},
            {"name": "knowledge-db", "owner": "knowledge", "migrations": "created at start-up",
             "tables": knowledge_tables()},
            {"name": "assistant-db", "owner": "assistant",
             "migrations": "created at start-up; LangGraph creates checkpoints, "
                           "checkpoint_blobs, checkpoint_writes, checkpoint_migrations",
             "tables": assistant_tables()},
        ]
    }


# -- catalog -----------------------------------------------------------------------------


def _emitters() -> dict[str, list[str]]:
    """topic -> the modules that call `emit(session, "<topic>", ...)`."""
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "emit"
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
                found.setdefault(node.args[1].value, set()).add(
                    path.relative_to(ROOT).as_posix())
    return {topic: sorted(paths) for topic, paths in found.items()}


#: What each Part A consumer and workflow does, in one line. The handlers carry no
#: docstrings of their own; a consumer or workflow missing here fails generation, so a new
#: one cannot reach the site undescribed.
DESCRIPTIONS = {
    "analytics.daily-rollup": "Adds each booking, cancellation, no-show and completed visit "
                              "to the day's counts in analytics_daily",
    "notifications.patient-messages": "Turns events a patient should hear about into one "
                                      "notification row each, sent by Celery after commit",
    "scheduling.cancel-follow-up": "Starts CancelFollowUpWorkflow: stop the visit timer, "
                                   "refund, offer the slot to the waitlist",
    "scheduling.post-visit": "Starts PostVisitWorkflow: capture the booking's payment",
    "scheduling.provider-deactivation": "Starts ProviderDeactivationWorkflow when a provider "
                                        "goes inactive: cancel their future appointments",
    "BookAppointmentWorkflow": "Booking: create pending, validate, claim the slot, billing "
                               "pre-check, schedule reminders, confirm last; compensate on failure",
    "CanaryWorkflow": "Readiness probe: proves a worker is polling and executing",
    "notifications.send": "Send one notification: claim the row, throttle per patient in "
                          "Redis, email (Mailpit) or SMS, retry with backoff, dead-letter",
    "scheduling.reconcile_stale_pending": "Fail bookings stuck in pending whose workflow is "
                                          "gone: free the slot, release the payment hold",
    "analytics.reconcile": "Recount the last days from scheduling's tables and report any "
                           "drift from analytics_daily (reports; never overwrites)",
    "clinical.extract_document": "Extract the text of an uploaded report (PDF) for review",
}

#: Part B's consumers live in their own services; they are listed from their code.
PART_B_CONSUMERS = [
    {"name": "knowledge.provider-directory", "service": "knowledge",
     "topics": ["providers.state"],
     "does": "Keeps the knowledge service's own provider directory and indexes each "
             "active provider into Qdrant",
     "code": "services/knowledge/knowledge_service/consumer.py"},
    {"name": "assistant.revocations", "service": "assistant",
     "topics": ["identity.sessions_revoked"],
     "does": "Fills the assistant's deny-list: tokens issued before a revocation are refused",
     "code": "services/assistant/assistant_service/main.py"},
    {"name": "assistant.conversions", "service": "assistant",
     "topics": ["appointments.booked"],
     "does": "Records every booking with its origin (assistant or not) for the conversion rate",
     "code": "services/assistant/assistant_service/main.py"},
]


def catalog() -> dict:
    import importlib

    from app.celery_app import TASK_MODULES, TASKS, celery

    for module in TASK_MODULES:  # tasks register on import; the worker imports them itself
        importlib.import_module(module)
    from app.components import register_all
    from app.core.temporal import ACTIVITIES, WORKFLOWS
    from app.events import catalog as events
    from app.events.consumer import CONSUMERS

    register_all()
    emitters = _emitters()
    topics = [{
        "name": t.name, "kind": t.kind.value, "schema_version": t.schema_version,
        "partitions": t.partitions, "emitted_by": emitters.get(t.name, []),
        "schema": f"contracts/events/{t.name}.avsc",
    } for t in sorted(events.TOPICS.values(), key=lambda t: t.name)]
    consumers = [{
        "name": spec.name, "service": "part-a consumers", "topics": sorted(spec.topics),
        "does": DESCRIPTIONS[spec.name],
        "code": spec.handler.__module__.replace(".", "/") + ".py",
    } for spec in sorted(CONSUMERS, key=lambda s: s.name)] + PART_B_CONSUMERS
    routes = celery.conf.task_routes or {}
    schedule = {entry["task"]: str(entry["schedule"])
                for entry in (celery.conf.beat_schedule or {}).values()}
    tasks = [{
        "name": name, "queue": routes.get(name, {}).get("queue", celery.conf.task_default_queue),
        "schedule": schedule.get(name, ""),
        "does": _first_line(getattr(getattr(task, "run", None), "__doc__", ""))
        or DESCRIPTIONS[name],
    } for name, task in sorted(TASKS.items())]
    workflows = [{"name": name, "type": getattr(cls, "__temporal_workflow_definition").name,
                  "does": _first_line(cls.__doc__) or DESCRIPTIONS[name]}
                 for name, cls in sorted(WORKFLOWS.items())]
    activities = sorted(ACTIVITIES.names())
    return {"topics": topics, "consumers": consumers, "celery_tasks": tasks,
            "temporal_workflows": workflows, "temporal_activities": activities}


# -- openapi -----------------------------------------------------------------------------


def openapi() -> dict:
    from app.main import create_app

    return create_app().openapi()


# -- main --------------------------------------------------------------------------------


def render() -> dict[str, str]:
    _ensure_paths()
    return {name: json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            for name, data in (("schema.json", schema()), ("catalog.json", catalog()),
                               ("openapi.json", openapi()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/gen_docs_data.py")
    parser.add_argument("--check", action="store_true", help="fail if the files are stale")
    args = parser.parse_args(argv)
    stale = []
    rendered = render()
    for name, text in rendered.items():
        path = OUT / name
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != text:
            stale.append(name)
            if not args.check:
                OUT.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    if args.check and stale:
        print(f"stale: {', '.join(stale)} -- run python scripts/gen_docs_data.py")
        return 1
    print(f"{'checked' if args.check else 'written'}: {', '.join(sorted(rendered))}"
          + (f" (updated {', '.join(stale)})" if stale and not args.check else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
