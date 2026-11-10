"""The knowledge process: the gRPC server and the provider-directory consumer.

    python -m knowledge_service.main      (from services/knowledge)
"""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from knowledge_service.auth import TokenVerifier
from knowledge_service.consumer import run as run_consumer
from knowledge_service.server import SERVING, build_server
from knowledge_service.settings import KnowledgeSettings
from knowledge_service.store import PostgresDirectory
from knowledge_service.telemetry import setup as setup_tracing

logger = logging.getLogger("knowledge")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = KnowledgeSettings()
    tracing = setup_tracing(settings.otel_endpoint_http, settings.environment)
    engine = create_async_engine(settings.database_url)
    directory = PostgresDirectory(engine)
    await directory.create()
    server = build_server(directory, TokenVerifier(settings.jwks_url, settings.jwt_issuer),
                          settings.grpc_port)
    await server.start()
    # Ready only once the vector database answers too: Week 4 depends on it.
    async with httpx.AsyncClient() as client:
        (await client.get(f"{settings.qdrant_url}/readyz", timeout=5)).raise_for_status()
    await server.health.set("", SERVING)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            break
    logger.info("knowledge serving on %s", settings.grpc_port)
    consumer = asyncio.create_task(run_consumer(settings, directory, stop))
    await stop.wait()
    await server.stop(5)
    await consumer
    await engine.dispose()
    if tracing:
        tracing.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
