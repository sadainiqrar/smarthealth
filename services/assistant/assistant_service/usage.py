"""LLM usage per turn, and its totals per user for the admin's usage page (PART-B-FR-5).

Every finished turn writes one row: who (the token's subject and role -- the assistant holds
no names or emails; the admin's browser joins them from Part A's `GET /users`), which
provider and model, how many model calls and tokens, time to first token and to the end,
and how the turn ended. Turns answered by a guided flow alone have zero model calls, so the
page shows how much of the assistant's work needs a model at all.

Cost is an *estimate*: tokens times the configured price per million
(`ASSISTANT_LLM_PRICE_INPUT_PER_MTOK` / `_OUTPUT_`), which the operator keeps in step with
the provider's price list.

In-memory and Postgres implementations behind one interface, as everywhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

SCHEMA = """
create table if not exists turn_usage (
    user_id text not null,
    turn_id text not null,
    role text not null,
    conversation_id text not null,
    provider text not null,
    model text not null,
    model_calls integer not null,
    input_tokens integer not null,
    output_tokens integer not null,
    first_token_ms integer,
    total_ms integer not null,
    outcome text not null,
    created_at timestamptz not null default now(),
    primary key (user_id, turn_id)
);
create index if not exists ix_turn_usage_created on turn_usage (created_at);
create table if not exists drafts (
    user_id text not null,
    draft_id text not null,
    kind text not null,
    status text not null,
    recipients integer not null default 0,
    created_at timestamptz not null default now(),
    decided_at timestamptz,
    primary key (user_id, draft_id)
);
"""


@dataclass
class DraftTotals:
    generated: int = 0
    sent: int = 0
    discarded: int = 0
    recipients: int = 0


@dataclass
class TurnUsage:
    """Filled in while a turn runs (agent.py adds each model call)."""

    started: float = field(default_factory=time.monotonic)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    first_token_ms: int | None = None

    def first_token(self) -> None:
        if self.first_token_ms is None:
            self.first_token_ms = int((time.monotonic() - self.started) * 1000)

    def add_call(self, usage: dict | None) -> None:
        self.model_calls += 1
        if usage:
            self.input_tokens += int(usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


@dataclass
class UsageRow:
    user_id: str
    turn_id: str
    role: str
    conversation_id: str
    provider: str
    model: str
    model_calls: int
    input_tokens: int
    output_tokens: int
    first_token_ms: int | None
    total_ms: int
    outcome: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class UserTotals:
    user_id: str
    role: str
    conversations: int = 0
    turns: int = 0
    model_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    avg_first_token_ms: float | None = None
    avg_total_ms: float | None = None
    errors: int = 0
    last_active: datetime | None = None


def outcome_of(events: list[dict]) -> str:
    if any(e["kind"] == "error" for e in events):
        return "error"
    done = next((e for e in reversed(events) if e["kind"] == "done"), {})
    return {"cancelled": "cancelled", "awaiting_input": "awaiting_input"}.get(
        done.get("reason", ""), "answered"
    )


def window(start: str, end: str) -> tuple[datetime, datetime]:
    """[start, end) as instants; dates are inclusive, defaulting to the last seven days."""
    last = date.fromisoformat(end) if end else datetime.now(UTC).date()
    first = date.fromisoformat(start) if start else last - timedelta(days=6)
    to_instant = lambda d: datetime(d.year, d.month, d.day, tzinfo=UTC)  # noqa: E731
    return to_instant(first), to_instant(last + timedelta(days=1))


class UsageStore(Protocol):
    async def record(self, row: UsageRow) -> None: ...
    async def per_user(self, start: datetime, end: datetime) -> list[UserTotals]: ...
    async def draft(
        self, user_id: str, draft_id: str, kind: str, status: str, recipients: int
    ) -> None: ...
    async def draft_totals(self, start: datetime, end: datetime) -> DraftTotals: ...


@dataclass
class InMemoryUsage:
    rows: dict[tuple[str, str], UsageRow] = field(default_factory=dict)
    drafts: dict[tuple[str, str], dict] = field(default_factory=dict)

    async def draft(
        self, user_id: str, draft_id: str, kind: str, status: str, recipients: int
    ) -> None:
        row = self.drafts.setdefault(
            (user_id, draft_id),
            {"kind": kind, "status": status, "recipients": 0, "created_at": datetime.now(UTC)},
        )
        if status != "generated":
            row.update(status=status, recipients=recipients)

    async def draft_totals(self, start: datetime, end: datetime) -> DraftTotals:
        rows = [r for r in self.drafts.values() if start <= r["created_at"] < end]
        return DraftTotals(
            generated=len(rows),
            sent=sum(1 for r in rows if r["status"] == "sent"),
            discarded=sum(1 for r in rows if r["status"] == "discarded"),
            recipients=sum(r["recipients"] for r in rows),
        )

    async def record(self, row: UsageRow) -> None:
        self.rows.setdefault((row.user_id, row.turn_id), row)

    async def per_user(self, start: datetime, end: datetime) -> list[UserTotals]:
        grouped: dict[tuple[str, str], list[UsageRow]] = {}
        for row in self.rows.values():
            if start <= row.created_at < end:
                grouped.setdefault((row.user_id, row.role), []).append(row)
        out = []
        for (user_id, role), rows in grouped.items():
            firsts = [r.first_token_ms for r in rows if r.first_token_ms is not None]
            out.append(
                UserTotals(
                    user_id=user_id,
                    role=role,
                    conversations=len({r.conversation_id for r in rows}),
                    turns=len(rows),
                    model_turns=sum(1 for r in rows if r.model_calls),
                    input_tokens=sum(r.input_tokens for r in rows),
                    output_tokens=sum(r.output_tokens for r in rows),
                    avg_first_token_ms=sum(firsts) / len(firsts) if firsts else None,
                    avg_total_ms=sum(r.total_ms for r in rows) / len(rows),
                    errors=sum(1 for r in rows if r.outcome == "error"),
                    last_active=max(r.created_at for r in rows),
                )
            )
        return sorted(out, key=lambda t: t.input_tokens + t.output_tokens, reverse=True)


class PostgresUsage:
    def __init__(self, pool) -> None:  # psycopg_pool.AsyncConnectionPool
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def record(self, row: UsageRow) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into turn_usage (user_id, turn_id, role, conversation_id, provider,"
                " model, model_calls, input_tokens, output_tokens, first_token_ms, total_ms,"
                " outcome) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict do nothing",
                (
                    row.user_id,
                    row.turn_id,
                    row.role,
                    row.conversation_id,
                    row.provider,
                    row.model,
                    row.model_calls,
                    row.input_tokens,
                    row.output_tokens,
                    row.first_token_ms,
                    row.total_ms,
                    row.outcome,
                ),
            )

    async def draft(
        self, user_id: str, draft_id: str, kind: str, status: str, recipients: int
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into drafts (user_id, draft_id, kind, status, recipients)"
                " values (%s, %s, %s, %s, %s) on conflict (user_id, draft_id) do update set"
                " status = excluded.status, recipients = excluded.recipients,"
                " decided_at = now() where excluded.status <> 'generated'",
                (user_id, draft_id, kind, status, recipients),
            )

    async def draft_totals(self, start: datetime, end: datetime) -> DraftTotals:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select count(*), count(*) filter (where status = 'sent'),"
                    " count(*) filter (where status = 'discarded'), coalesce(sum(recipients), 0)"
                    " from drafts where created_at >= %s and created_at < %s",
                    (start, end),
                )
            ).fetchone()
        return DraftTotals(*[int(v) for v in row])

    async def per_user(self, start: datetime, end: datetime) -> list[UserTotals]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select user_id, role, count(distinct conversation_id), count(*),"
                    " count(*) filter (where model_calls > 0), sum(input_tokens),"
                    " sum(output_tokens), avg(first_token_ms), avg(total_ms),"
                    " count(*) filter (where outcome = 'error'), max(created_at)"
                    " from turn_usage where created_at >= %s and created_at < %s"
                    " group by user_id, role"
                    " order by sum(input_tokens) + sum(output_tokens) desc",
                    (start, end),
                )
            ).fetchall()
        return [
            UserTotals(
                user_id=r[0],
                role=r[1],
                conversations=r[2],
                turns=r[3],
                model_turns=r[4],
                input_tokens=int(r[5] or 0),
                output_tokens=int(r[6] or 0),
                avg_first_token_ms=float(r[7]) if r[7] is not None else None,
                avg_total_ms=float(r[8]) if r[8] is not None else None,
                errors=r[9],
                last_active=r[10],
            )
            for r in rows
        ]
