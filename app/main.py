"""FastAPI application entry point.

Resources are created in the lifespan handler and stored on `app.state`. Every client
is lazy — none of them connects at startup — so the application boots with no
infrastructure running, and the T1 contract lane can exercise the real startup path
without Docker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.logging import configure_logging
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client
from app.db.redis import create_redis_client
from app.db.session import create_session_factory
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()

    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.mongo = create_mongo_client(settings)
    app.state.redis = create_redis_client(settings)
    try:
        yield
    finally:
        await app.state.engine.dispose()
        # `close()` is a coroutine on pymongo's async client, unlike Motor's.
        await app.state.mongo.close()
        await app.state.redis.aclose()


def create_app() -> FastAPI:
    application = FastAPI(title="SmartHealth", version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)
    return application


app = create_app()
