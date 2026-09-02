"""Router assembly. Every module's router is mounted here, and only here."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)
