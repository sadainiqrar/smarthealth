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
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.api.error_handlers import register_error_handlers
from app.api.router import api_router
from app.core.logging import configure_logging
from app.core.ratelimit import RedisRateLimiter
from app.core.telemetry import instrument_engine, setup_telemetry
from app.core.temporal import connect_client
from app.db.engine import create_engine
from app.db.mongo import create_mongo_client
from app.db.redis import create_redis_client
from app.db.session import create_session_factory
from app.modules.clinical.storage import FilesystemDocumentStore
from app.celery_app import enqueue_send
from app.modules.clinical.tasks import enqueue_extraction
from app.modules.identity.sessions import RedisSessionStore
from app.settings import DEV_SIGNING_KEY, Settings, get_settings


def _refuse_the_development_key_in_production(settings: Settings) -> None:
    """The development signing key is in the repository; in production anyone could
    mint an admin token with it. Failing to start is the only safe answer."""
    if settings.environment == "production" and settings.jwt_private_key == DEV_SIGNING_KEY:
        raise RuntimeError(
            "SMARTHEALTH_JWT_PRIVATE_KEY is the development key; set a real one "
            "(python -m app.cli generate-signing-key)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _refuse_the_development_key_in_production(settings)
    configure_logging(settings.log_level)
    app.state.telemetry = setup_telemetry(settings, "part-a-api")

    app.state.settings = settings
    app.state.engine = create_engine(settings)
    instrument_engine(app.state.engine)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.mongo = create_mongo_client(settings)
    app.state.redis = create_redis_client(settings)
    app.state.sessions = RedisSessionStore(app.state.redis, settings)
    app.state.rate_limiter = RedisRateLimiter(app.state.redis, settings)
    app.state.documents = FilesystemDocumentStore.from_settings(settings)
    app.state.enqueue_extraction = enqueue_extraction
    app.state.enqueue_send = enqueue_send
    # Lazy: the API boots with Temporal down, like every client above. Nothing to
    # close on shutdown -- the SDK client exposes no close(); once /ready has used it,
    # its gRPC channel lives as long as the process.
    app.state.temporal = await connect_client(settings)
    try:
        yield
    finally:
        app.state.telemetry.flush()
        await app.state.engine.dispose()
        # `close()` is a coroutine on pymongo's async client, unlike Motor's.
        await app.state.mongo.close()
        await app.state.redis.aclose()


def create_app() -> FastAPI:
    application = FastAPI(title="SmartHealth", version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)
    register_error_handlers(application)
    # At creation, not in the lifespan: Starlette refuses new middleware once the app has
    # started. The instrumentation binds to the global proxy providers, which resolve to
    # the real ones as soon as the lifespan installs them. Probes are excluded: compose
    # polls them every few seconds, and a trace per poll would bury the real traffic.
    FastAPIInstrumentor.instrument_app(application, excluded_urls="/health$,/ready$")
    return application


app = create_app()
