"""From numbers to plain statuses (system health spec §4.3).

Every threshold is *at or above*: a value equal to `warn` is `warn`. The table is the one
the documentation quotes; change both together.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Status = Literal["ok", "warn", "bad", "idle", "unknown"]

#: A heartbeat older than this means the process is down.
HEARTBEAT_MAX_AGE_S = 60
#: A failure share needs this many runs before it can be warn or bad.
MIN_EVENTS = 5
FAILURE_PCT = (5.0, 20.0)

#: key -> (warn, bad, unit, what "ok" is called)
THRESHOLDS: dict[str, tuple[float, float, str, str]] = {
    "outbox_age_s": (60, 300, "s", "normal"),
    "consumer_lag": (100, 1000, "events", "keeping up"),
    "queue_ready": (100, 1000, "messages", "normal"),
    "dead_letters": (1, 10, "", "none"),
    "first_token_p95_s": (5, 15, "s", "fast"),
    "retrieval_no_results_pct": (25, 50, "%", "normal"),
}

_RANK = {"bad": 4, "warn": 3, "unknown": 2, "ok": 1, "idle": 0}


@dataclass(frozen=True)
class Verdict:
    status: Status
    reason: str


def heartbeat(age_seconds: float | None) -> Verdict:
    if age_seconds is None:
        return Verdict("bad", "not reporting")
    if age_seconds >= HEARTBEAT_MAX_AGE_S:
        return Verdict("bad", f"last seen {age_seconds / 60:.0f} min ago")
    return Verdict("ok", f"alive, seen {age_seconds:.0f} s ago")


def failure_share(total: float | None, failed: float | None) -> Verdict:
    if total is None or failed is None:
        return Verdict("unknown", "metrics unavailable")
    total, failed = int(total), int(failed)
    if total == 0:
        return Verdict("idle", "no runs in this window")
    if total < MIN_EVENTS:
        if failed:
            return Verdict("ok", f"{failed} of {total} failed (too few to judge)")
        return Verdict("ok", f"{total} runs, none failed")
    pct = failed / total * 100
    warn, bad = FAILURE_PCT
    status: Status = "bad" if pct >= bad else "warn" if pct >= warn else "ok"
    if not failed:
        return Verdict("ok", f"{total} runs, none failed")
    return Verdict(status, f"{failed} of {total} failed ({pct:.1f}%)")


def level(key: str, value: float | None) -> Verdict:
    warn, bad, unit, fine = THRESHOLDS[key]
    if value is None:
        return Verdict("unknown", "metrics unavailable")
    shown = f"{value:g}{' ' + unit if unit and unit != '%' else unit}"
    dead = key == "dead_letters"
    if value >= bad:
        return Verdict("bad", f"{shown} dead letters" if dead else f"{shown}: far behind")
    if value >= warn:
        return Verdict("warn", f"{shown} dead letters" if dead else f"{shown}: falling behind")
    return Verdict("ok", fine if value == 0 else f"{shown}: {fine}")


def worst(statuses: Iterable[Status]) -> Status:
    ranked = sorted(statuses, key=_RANK.__getitem__, reverse=True)
    return ranked[0] if ranked else "unknown"
