"""FastAPI application entry point."""

from __future__ import annotations

from fastapi import FastAPI

from app.settings import get_settings

app = FastAPI(title="SmartHealth", version="0.1.0")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness/readiness probe. The test stack and the first case both assert on it."""
    settings = get_settings()
    return {"status": "ok", "environment": settings.environment, "service": settings.service_name}
