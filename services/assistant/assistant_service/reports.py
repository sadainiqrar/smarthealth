"""Reports written in the background and streamed as they are written (PART-B-FR-4).

"Weekly operations report" is long: numbers from Part A's analytics, a narrative from the
model. It must not hold a request open (NFR-1), and the admin who asked may close the tab
and come back. So `StartReport` returns a job id at once, and the report runs as its own
task: every event (steps, cards, prose) is appended to the report's Redis Stream as it is
produced, and `WatchReport` replays that stream and follows it until the report is done --
from any tab, any number of times, resuming after a dropped connection.

**Every number comes from Part A, never from the model** (spec 9.1): the figures are fetched
first and shown as cards; the model is given only those figures and asked to explain them.
With no model the narrative is a plain statement of the same figures.

A finished report is kept in the assistant's database (title, status, its events), so the
list survives the stream's TTL and a restart. As built, a report runs in the assistant
process (an asyncio task), not a separate worker: a restart mid-report marks it failed, and
it is started again. Spec 9.1 names an `assistant-worker`; one process is enough at this
scale, and the job/stream split is what a worker would need, unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from assistant_service import metrics
from assistant_service.auth import Caller
from assistant_service.parta import PartA, PartAError, PartAUnavailable
from assistant_service.stores import Stores
from assistant_service.usage import TurnUsage

logger = logging.getLogger(__name__)

KINDS = {
    "operations_7": ("Weekly operations report", 7),
    "operations_30": ("Monthly operations report", 30),
    "utilisation_30": ("Department utilisation report", 30),
}
METRICS = ("booked", "completed", "cancelled", "no_show")

NARRATIVE_INSTRUCTIONS = (
    "You write the narrative of an operations report for MediNova clinic managers. You are "
    "given figures for a period and the period before it, and daily counts. Explain what "
    "changed and what stands out, in plain business language, in 3 short paragraphs and "
    "then 2-4 bullet points of things worth looking into. Use only the figures given: never "
    "invent a number, a cause you cannot see in the data, or a department. Say plainly when "
    "the data is too thin to draw a conclusion. Markdown, no headings, no tables."
)

UTILISATION_INSTRUCTIONS = (
    "You write the narrative of a department utilisation report for MediNova clinic "
    "managers: for each department, how many of the appointment slots it offered were booked. "
    "In 2 short paragraphs and 2-4 bullet points, say which departments are busiest and "
    "quietest and where capacity might be moved, using only the figures given -- never invent "
    "a number or a cause. Say plainly when the data is too thin to conclude. Markdown, no "
    "headings, no tables."
)

SCHEMA = """
create table if not exists reports (
    user_id text not null,
    id text not null,
    kind text not null,
    title text not null,
    status text not null,
    period_from date not null,
    period_to date not null,
    events jsonb,
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    primary key (user_id, id)
);
"""


@dataclass
class Report:
    id: str
    user_id: str
    kind: str
    title: str
    status: str  # running | done | failed
    period_from: date
    period_to: date
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    events: list[dict] | None = None


class ReportStore(Protocol):
    async def create(self, report: Report) -> None: ...
    async def finish(
        self, user_id: str, report_id: str, status: str, events: list[dict]
    ) -> None: ...
    async def get(self, user_id: str, report_id: str) -> Report | None: ...
    async def list(self, user_id: str, limit: int) -> list[Report]: ...
    async def fail_running(self) -> int: ...


@dataclass
class InMemoryReports:
    reports: dict[tuple[str, str], Report] = field(default_factory=dict)

    async def create(self, report: Report) -> None:
        self.reports[(report.user_id, report.id)] = report

    async def finish(self, user_id: str, report_id: str, status: str, events: list[dict]) -> None:
        report = self.reports[(user_id, report_id)]
        report.status, report.events = status, events

    async def get(self, user_id: str, report_id: str) -> Report | None:
        return self.reports.get((user_id, report_id))

    async def list(self, user_id: str, limit: int) -> list[Report]:
        mine = [r for (u, _), r in self.reports.items() if u == user_id]
        return sorted(mine, key=lambda r: r.created_at, reverse=True)[:limit]

    async def fail_running(self) -> int:
        running = [r for r in self.reports.values() if r.status == "running"]
        for r in running:
            r.status = "failed"
        return len(running)


class PostgresReports:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def create(self, report: Report) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into reports (user_id, id, kind, title, status, period_from, period_to)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    report.user_id,
                    report.id,
                    report.kind,
                    report.title,
                    report.status,
                    report.period_from,
                    report.period_to,
                ),
            )

    async def finish(self, user_id: str, report_id: str, status: str, events: list[dict]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "update reports set status = %s, events = %s, finished_at = now()"
                " where user_id = %s and id = %s",
                (status, json.dumps(events), user_id, report_id),
            )

    def _row(self, r) -> Report:
        events = r[8] if isinstance(r[8], list) or r[8] is None else json.loads(r[8])
        return Report(
            id=r[0],
            user_id=r[1],
            kind=r[2],
            title=r[3],
            status=r[4],
            period_from=r[5],
            period_to=r[6],
            created_at=r[7],
            events=events,
        )

    async def get(self, user_id: str, report_id: str) -> Report | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select id, user_id, kind, title, status, period_from, period_to, created_at,"
                    " events from reports where user_id = %s and id = %s",
                    (user_id, report_id),
                )
            ).fetchone()
        return self._row(row) if row else None

    async def list(self, user_id: str, limit: int) -> list[Report]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, user_id, kind, title, status, period_from, period_to, created_at,"
                    " null from reports where user_id = %s order by created_at desc limit %s",
                    (user_id, limit),
                )
            ).fetchall()
        return [self._row(r) for r in rows]

    async def fail_running(self) -> int:
        async with self._pool.connection() as conn:
            done = await conn.execute(
                "update reports set status = 'failed', finished_at = now() where status = 'running'"
            )
        return done.rowcount


def _pct(now: float | None, before: float | None) -> str:
    if now is None:
        return "no data"
    if not before:
        return f"{now:g}"
    change = (now - before) / before * 100
    return f"{now:g} ({'+' if change >= 0 else ''}{change:.0f}% vs the period before)"


class ReportRunner:
    """Starts reports as background tasks and records what they stream."""

    def __init__(
        self,
        stores: Stores,
        reports: ReportStore,
        parta: PartA,
        model: BaseChatModel | None,
        on_usage: Callable[[Caller, str, TurnUsage, list[dict]], object] | None = None,
    ) -> None:
        self._stores = stores
        self._reports = reports
        self._parta = parta
        self._model = model
        self._on_usage = on_usage
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def stream_key(report_id: str) -> str:
        return f"report-{report_id}"

    async def start(
        self, caller: Caller, kind: str, start: date | None, end: date | None
    ) -> Report:
        title, days = KINDS[kind]
        last = end or datetime.now(UTC).date()
        first = start or last - timedelta(days=days - 1)
        report = Report(
            id=uuid.uuid4().hex,
            user_id=caller.user_id,
            kind=kind,
            title=title,
            status="running",
            period_from=first,
            period_to=last,
        )
        await self._reports.create(report)
        task = asyncio.create_task(self._run(caller, report))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return report

    async def wait(self) -> None:
        """For tests and shutdown: every running report has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, caller: Caller, report: Report) -> None:
        key = self.stream_key(report.id)
        events: list[dict] = []
        usage = TurnUsage()

        async def emit(event: dict) -> None:
            events.append(event)
            await self._stores.append(key, len(events), event)

        status = "done"
        try:
            await self._write(caller, report, emit, usage)
            await emit({"kind": "done", "reason": "complete"})
        except (PartAError, PartAUnavailable) as exc:
            status = "failed"
            await emit(
                {
                    "kind": "error",
                    "code": "unavailable",
                    "message": f"Couldn't fetch the figures: {exc}",
                }
            )
            await emit({"kind": "done", "reason": "complete"})
        except Exception:
            logger.exception("report failed", extra={"report_id": report.id})
            status = "failed"
            await emit({"kind": "error", "code": "internal", "message": "The report failed."})
            await emit({"kind": "done", "reason": "complete"})
        finally:
            metrics.record_report(report.kind, status)
            await self._reports.finish(caller.user_id, report.id, status, events)
            if self._on_usage is not None:
                await self._on_usage(caller, report.id, usage, events)

    async def _write(self, caller: Caller, report: Report, emit, usage: TurnUsage) -> None:
        if report.kind.startswith("utilisation"):
            await self._write_utilisation(caller, report, emit, usage)
            return
        token = caller.token
        days = (report.period_to - report.period_from).days + 1
        before_to = report.period_from - timedelta(days=1)
        before_from = before_to - timedelta(days=days - 1)
        call = "figures-1"
        await emit(
            {
                "kind": "tool",
                "call_id": call,
                "name": "analytics",
                "label": "Fetching the figures",
                "status": "running",
                "summary": "",
            }
        )
        now, before, *series = await asyncio.gather(
            self._parta.analytics_summary(
                token, start=report.period_from.isoformat(), end=report.period_to.isoformat()
            ),
            self._parta.analytics_summary(
                token, start=before_from.isoformat(), end=before_to.isoformat()
            ),
            *[
                self._parta.analytics_series(
                    token, m, start=report.period_from.isoformat(), end=report.period_to.isoformat()
                )
                for m in METRICS
            ],
        )
        await emit(
            {
                "kind": "tool",
                "call_id": call,
                "name": "analytics",
                "label": "Fetching the figures",
                "status": "done",
                "summary": f"{days} days and the {days} before",
            }
        )

        def rate(value: float | None) -> float | None:
            return None if value is None else round(value * 100, 1)

        def minutes(value: float | None) -> float | None:
            return None if value is None else round(value / 60, 1)

        figures = [
            ("Appointments booked", now["appointments_booked"], before["appointments_booked"]),
            ("Completed visits", now["completed_visits"], before["completed_visits"]),
            ("Cancellations", now["cancellations"], before["cancellations"]),
            ("No-shows", now["no_shows"], before["no_shows"]),
            (
                "Cancellation rate %",
                rate(now["cancellation_rate"]),
                rate(before["cancellation_rate"]),
            ),
            (
                "Average wait (min)",
                minutes(now["average_wait_seconds"]),
                minutes(before["average_wait_seconds"]),
            ),
        ]
        await emit(
            {
                "kind": "card",
                "card_kind": "metrics",
                "title": f"{report.period_from:%d %b} – {report.period_to:%d %b %Y}",
                "subtitle": f"Compared with {before_from:%d %b} – {before_to:%d %b}",
                "fields": [{"label": n, "value": _pct(a, b)} for n, a, b in figures]
                + [{"label": "Registered patients", "value": str(now["total_patients"])}],
                "badge": f"{days} days",
                "link": "",
            }
        )
        daily: dict[str, dict[str, float]] = {}
        for metric, points in zip(METRICS, series, strict=True):
            for p in points:
                daily.setdefault(p["day"], {})[metric] = p["value"]
        busiest = max(daily.items(), key=lambda kv: kv[1].get("booked") or 0, default=None)

        material = "\n".join(f"{n}: this period {a}, period before {b}" for n, a, b in figures)
        material += "\nDaily (day: booked/completed/cancelled/no-show):\n" + "\n".join(
            f"{d}: " + "/".join(str(int(v.get(m) or 0)) for m in METRICS)
            for d, v in sorted(daily.items())
        )
        await self._narrative(emit, usage, material, figures, busiest)

    async def _write_utilisation(
        self, caller: Caller, report: Report, emit, usage: TurnUsage
    ) -> None:
        call = "utilisation-1"
        await emit(
            {
                "kind": "tool",
                "call_id": call,
                "name": "analytics",
                "label": "Fetching slot use by department",
                "status": "running",
                "summary": "",
            }
        )
        rows = await self._parta.analytics_utilisation(
            caller.token, start=report.period_from.isoformat(), end=report.period_to.isoformat()
        )
        await emit(
            {
                "kind": "tool",
                "call_id": call,
                "name": "analytics",
                "label": "Fetching slot use by department",
                "status": "done",
                "summary": f"{len(rows)} departments",
            }
        )
        if not rows:
            await emit(
                {
                    "kind": "token",
                    "text": "No slots were offered in this period, so "
                    "there is no utilisation to report.",
                }
            )
            return

        def share(row: dict) -> str:
            pct = "—" if row["utilisation"] is None else f"{row['utilisation'] * 100:.0f}%"
            return f"{row['booked']} of {row['slots']} slots ({pct})"

        offered = sum(r["slots"] for r in rows)
        taken = sum(r["booked"] for r in rows)
        await emit(
            {
                "kind": "card",
                "card_kind": "metrics",
                "title": f"{report.period_from:%d %b} – {report.period_to:%d %b %Y}",
                "subtitle": f"Overall: {taken} of {offered} slots booked"
                f" ({taken / offered * 100:.0f}%)"
                if offered
                else "",
                "fields": [
                    {"label": f"{r['department']} · {r['clinic']}", "value": share(r)}
                    for r in rows[:15]
                ],
                "badge": "busiest first",
                "link": "",
            }
        )
        material = (
            f"Period: {report.period_from} to {report.period_to}. Overall {taken} of "
            f"{offered} offered slots booked.\nPer department (clinic), booked of "
            "offered:\n" + "\n".join(f"{r['department']} ({r['clinic']}): {share(r)}" for r in rows)
        )
        await self._narrative(emit, usage, material, [], None, kind="utilisation")

    async def _narrative(
        self, emit, usage: TurnUsage, material: str, figures, busiest, kind: str = "operations"
    ) -> None:
        if self._model is not None:
            try:
                whole = None
                instructions = (
                    UTILISATION_INSTRUCTIONS if kind == "utilisation" else NARRATIVE_INSTRUCTIONS
                )
                async for chunk in self._model.astream(
                    [SystemMessage(instructions), HumanMessage(material)]
                ):
                    whole = chunk if whole is None else whole + chunk
                    if isinstance(chunk.content, str) and chunk.content:
                        usage.first_token()
                        await emit({"kind": "token", "text": chunk.content})
                if whole is not None:
                    usage.add_call(getattr(whole, "usage_metadata", None))
                if usage.first_token_ms is not None:
                    return
            except Exception:  # noqa: BLE001 - the report still states its figures
                logger.info("report narrative failed; writing it plainly", exc_info=True)
        if kind == "utilisation":
            text = (
                "Departments are listed busiest first above. Every figure is booked "
                "slots out of the slots the department offered in the period."
            )
            for word in text.split(" "):
                await emit({"kind": "token", "text": word + " "})
            return
        lines = [f"- **{n}**: {_pct(a, b)}" for n, a, b in figures[:4]]
        if busiest:
            lines.append(
                f"- The busiest day was {busiest[0]}, with "
                f"{int(busiest[1].get('booked') or 0)} bookings."
            )
        text = "In this period:\n\n" + "\n".join(lines) + "\n"
        for word in text.split(" "):
            await emit({"kind": "token", "text": word + " "})
