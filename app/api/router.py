"""Router assembly. Every module's router is mounted here, and only here."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health
from app.modules.analytics import router as analytics_router
from app.modules.clinical import router as clinical_router
from app.modules.identity import router as identity_router
from app.modules.notifications import router as notifications_router
from app.modules.patients import router as patients_router
from app.modules.providers import router as providers_router
from app.modules.scheduling import router as scheduling_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(identity_router.router)
api_router.include_router(identity_router.jwks_router)
api_router.include_router(identity_router.users_router)
api_router.include_router(analytics_router.router)
api_router.include_router(clinical_router.router)
api_router.include_router(notifications_router.router)
api_router.include_router(patients_router.router)
api_router.include_router(providers_router.router)
api_router.include_router(scheduling_router.router)
api_router.include_router(scheduling_router.slots_router)
api_router.include_router(scheduling_router.waitlist_router)
