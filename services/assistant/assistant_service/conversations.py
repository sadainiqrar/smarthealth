"""Conversations and their transcripts: the threads a user comes back to (spec 9.4).

Each finished turn is recorded once -- what the user said or picked, and every event the
assistant streamed -- so the client rebuilds a thread by folding the same events it folded
live. The LangGraph checkpoint (agent.py) holds the *state* that resumes a paused flow; this
holds the *record* a person reads. Both live in the assistant's own Postgres
(`assistant-db`), keyed by user and conversation, so one user never lists or opens another's.

These rows are also what Part B analytics count (PART-B-FR-5): conversations, turns,
answered vs. errored, time to first token.

In-memory and Postgres implementations behind one interface, as everywhere else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

TITLE_LENGTH = 60

SCHEMA = """
create table if not exists conversations (
    user_id text not null,
    id text not null,
    title text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    turns integer not null default 0,
    awaiting_input boolean not null default false,
    primary key (user_id, id)
);
create index if not exists ix_conversations_recent on conversations (user_id, updated_at desc);
create table if not exists conversation_turns (
    user_id text not null,
    conversation_id text not null,
    turn_id text not null,
    position bigserial,
    user_text text not null,
    events jsonb not null,
    created_at timestamptz not null default now(),
    primary key (user_id, turn_id)
);
create index if not exists ix_turns_by_conversation
    on conversation_turns (user_id, conversation_id, position);
"""


def title_from(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1].rstrip() + "…"


@dataclass
class Summary:
    conversation_id: str
    title: str
    updated_at: datetime
    turns: int
    awaiting_input: bool


@dataclass
class Turn:
    turn_id: str
    user_text: str
    events: list[dict]


class ConversationStore(Protocol):
    async def record(self, user_id: str, conversation_id: str, turn_id: str, user_text: str,
                     events: list[dict]) -> None: ...
    async def list(self, user_id: str, limit: int) -> list[Summary]: ...
    async def get(self, user_id: str, conversation_id: str) -> tuple[str, list[Turn]] | None: ...
    async def delete(self, user_id: str, conversation_id: str) -> bool: ...


def _awaiting(events: list[dict]) -> bool:
    return bool(events) and events[-1].get("kind") == "done" and \
        events[-1].get("reason") == "awaiting_input"


@dataclass
class InMemoryConversations:
    conversations: dict[tuple[str, str], Summary] = field(default_factory=dict)
    turns: dict[tuple[str, str], list[Turn]] = field(default_factory=dict)

    async def record(self, user_id: str, conversation_id: str, turn_id: str, user_text: str,
                     events: list[dict]) -> None:
        key = (user_id, conversation_id)
        turns = self.turns.setdefault(key, [])
        if any(t.turn_id == turn_id for t in turns):
            return  # a turn is recorded once, however often it is replayed
        turns.append(Turn(turn_id, user_text, events))
        summary = self.conversations.get(key)
        now = datetime.now(UTC)
        if summary is None:
            title = title_from(user_text) or "New conversation"
            summary = self.conversations[key] = Summary(conversation_id, title, now, 0, False)
        summary.updated_at = now
        summary.turns = len(turns)
        summary.awaiting_input = _awaiting(events)

    async def list(self, user_id: str, limit: int) -> list[Summary]:
        mine = [s for (u, _), s in self.conversations.items() if u == user_id]
        return sorted(mine, key=lambda s: s.updated_at, reverse=True)[:limit]

    async def get(self, user_id: str, conversation_id: str) -> tuple[str, list[Turn]] | None:
        summary = self.conversations.get((user_id, conversation_id))
        if summary is None:
            return None
        return summary.title, list(self.turns.get((user_id, conversation_id), []))

    async def delete(self, user_id: str, conversation_id: str) -> bool:
        self.turns.pop((user_id, conversation_id), None)
        return self.conversations.pop((user_id, conversation_id), None) is not None


class PostgresConversations:
    def __init__(self, pool) -> None:  # psycopg_pool.AsyncConnectionPool
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def record(self, user_id: str, conversation_id: str, turn_id: str, user_text: str,
                     events: list[dict]) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            inserted = await conn.execute(
                "insert into conversation_turns (user_id, conversation_id, turn_id, user_text,"
                " events) values (%s, %s, %s, %s, %s) on conflict do nothing",
                (user_id, conversation_id, turn_id, user_text, json.dumps(events)))
            if inserted.rowcount == 0:
                return
            await conn.execute(
                "insert into conversations (user_id, id, title, turns, awaiting_input)"
                " values (%s, %s, %s, 1, %s)"
                " on conflict (user_id, id) do update set updated_at = now(),"
                " turns = conversations.turns + 1, awaiting_input = excluded.awaiting_input",
                (user_id, conversation_id, title_from(user_text) or "New conversation",
                 _awaiting(events)))

    async def list(self, user_id: str, limit: int) -> list[Summary]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "select id, title, updated_at, turns, awaiting_input from conversations"
                " where user_id = %s order by updated_at desc limit %s", (user_id, limit),
            )).fetchall()
        return [Summary(*row) for row in rows]

    async def get(self, user_id: str, conversation_id: str) -> tuple[str, list[Turn]] | None:
        async with self._pool.connection() as conn:
            head = await (await conn.execute(
                "select title from conversations where user_id = %s and id = %s",
                (user_id, conversation_id))).fetchone()
            if head is None:
                return None
            rows = await (await conn.execute(
                "select turn_id, user_text, events from conversation_turns"
                " where user_id = %s and conversation_id = %s order by position",
                (user_id, conversation_id))).fetchall()
        return head[0], [Turn(t, u, e if isinstance(e, list) else json.loads(e))
                         for t, u, e in rows]

    async def delete(self, user_id: str, conversation_id: str) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("delete from conversation_turns where user_id = %s"
                               " and conversation_id = %s", (user_id, conversation_id))
            gone = await conn.execute("delete from conversations where user_id = %s and id = %s",
                                      (user_id, conversation_id))
        return gone.rowcount > 0
