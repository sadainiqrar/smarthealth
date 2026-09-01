"""Executes a declarative case.

Step kinds and expectation kinds are dispatched through registries, so a later
phase adds a handler without touching this module's control flow. A kind with no
handler raises `NotImplementedError` — loudly, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.runner.schema import ApiExpect, Case, Step


@dataclass
class CaseContext:
    """Everything a step handler may touch. Later phases add db/kafka/temporal handles."""

    api: httpx.AsyncClient
    last_response: httpx.Response | None = None
    notes: dict[str, Any] = field(default_factory=dict)


StepHandler = Callable[[Step, CaseContext, str], Awaitable[None]]


async def _run_api_step(step: Step, ctx: CaseContext, where: str) -> None:
    spec = step.api
    assert spec is not None
    response = await ctx.api.request(
        spec.method, spec.path, json=spec.body, headers=spec.headers
    )
    ctx.last_response = response
    if spec.expect is not None:
        _assert_api(spec.expect, response, where)


STEP_HANDLERS: dict[str, StepHandler] = {"api": _run_api_step}


async def run_case(case: Case, ctx: CaseContext) -> None:
    """Execute every step, then assert the case-level expectations."""
    for index, step in enumerate(case.steps, start=1):
        where = f"case '{case.id}' step {index} ({step.kind})"
        handler = STEP_HANDLERS.get(step.kind)
        if handler is None:
            raise NotImplementedError(
                f"{where}: step kind '{step.kind}' has no engine handler yet. "
                f"Implement it, or mark the case status: blocked with a blocked_on reason."
            )
        await handler(step, ctx, where)

    if case.expect is None:
        return
    where = f"case '{case.id}' expect"
    for kind in sorted(case.expect.kinds):
        if kind == "api":
            assert ctx.last_response is not None, f"{where}: no API response was recorded"
            _assert_api(case.expect.api, ctx.last_response, where)
        else:
            raise NotImplementedError(
                f"{where}: expectation kind '{kind}' has no engine handler yet. "
                f"Implement it, or mark the case status: blocked with a blocked_on reason."
            )


def _assert_api(expect: ApiExpect, response: httpx.Response, where: str) -> None:
    if expect.status is not None:
        assert response.status_code == expect.status, (
            f"{where}: expected status {expect.status}, got {response.status_code} "
            f"(body: {response.text[:200]})"
        )
    if expect.json_contains is not None:
        _assert_contains(response.json(), expect.json_contains, where, "$")


def _assert_contains(actual: Any, expected: dict[str, Any], where: str, path: str) -> None:
    assert isinstance(actual, dict), (
        f"{where}: {path}: expected an object, got {type(actual).__name__}"
    )
    for key, wanted in expected.items():
        child = f"{path}.{key}"
        assert key in actual, f"{where}: {child}: missing from the response"
        if isinstance(wanted, dict):
            _assert_contains(actual[key], wanted, where, child)
        else:
            assert actual[key] == wanted, (
                f"{where}: {child}: expected {wanted!r}, got {actual[key]!r}"
            )
