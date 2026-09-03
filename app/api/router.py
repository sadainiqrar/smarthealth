"""Router assembly. Every module's router is mounted here, and only here."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health
from app.modules.identity import router as identity_router
from app.modules.patients import router as patients_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(identity_router.router)
api_router.include_router(patients_router.router)
