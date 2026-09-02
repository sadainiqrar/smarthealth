"""Liveness and readiness.

`/health` answers "is this process alive" and must stay a flat `dict[str, str]` —
FastAPI enforces that annotation as a response model, so a nested or boolean field
would raise at runtime.

`/ready` answers "can this process do useful work" and reports each dependency
separately, because "something is down" is far less actionable than "Mongo is down".
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.settings import get_settings

router = APIRouter(tags=["system"])

CHECK_TIMEOUT_SECONDS = 2.0


class DependencyStatus(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    ready: bool
    dependencies: list[DependencyStatus]


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. The test stack and the first case both assert on it."""
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "service": settings.service_name,
    }


def _summarise(exc: BaseException) -> str:
    """One readable line. Full tracebacks belong in logs, not in a probe response."""
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else exc.__class__.__name__)[:200]


async def _check_postgres(request: Request) -> DependencyStatus:
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # any failure means not ready
        return DependencyStatus(name="postgres", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="postgres", ok=True)


async def _check_mongo(request: Request) -> DependencyStatus:
    try:
        await request.app.state.mongo.admin.command("ping")
    except Exception as exc:
        return DependencyStatus(name="mongo", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="mongo", ok=True)


async def _check_redis(request: Request) -> DependencyStatus:
    try:
        await request.app.state.redis.ping()
    except Exception as exc:
        return DependencyStatus(name="redis", ok=False, detail=_summarise(exc))
    return DependencyStatus(name="redis", ok=True)


@router.get("/ready", response_model=ReadinessResponse)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    async def guarded(check) -> DependencyStatus:
        name = check.__name__.removeprefix("_check_")
        try:
            return await asyncio.wait_for(check(request), CHECK_TIMEOUT_SECONDS)
        except TimeoutError:
            return DependencyStatus(
                name=name, ok=False, detail=f"timed out after {CHECK_TIMEOUT_SECONDS}s"
            )

    dependencies = list(
        await asyncio.gather(
            guarded(_check_postgres), guarded(_check_mongo), guarded(_check_redis)
        )
    )
    all_ok = all(entry.ok for entry in dependencies)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(ready=all_ok, dependencies=dependencies)
