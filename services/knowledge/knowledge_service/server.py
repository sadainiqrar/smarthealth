"""The Knowledge gRPC service."""

from __future__ import annotations

import logging

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from smarthealth.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc

from knowledge_service.auth import TokenVerifier, Unauthenticated
from knowledge_service.eval import audiences_for
from knowledge_service.index import Index
from knowledge_service.store import Directory

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


class KnowledgeServicer(knowledge_pb2_grpc.KnowledgeServicer):
    def __init__(self, directory: Directory, verifier: TokenVerifier,
                 index: Index | None = None) -> None:
        self._directory = directory
        self._verifier = verifier
        self._index = index

    async def Search(self, request, context):  # noqa: N802 - gRPC method name
        metadata = dict(context.invocation_metadata())
        try:
            caller = self._verifier.verify(metadata.get("authorization"))
        except Unauthenticated as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        if not request.query.strip():
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is empty")
        limit = min(request.limit or DEFAULT_LIMIT, MAX_LIMIT)
        if self._index is not None:
            # The audience comes from the caller's token, never the request (spec 8.4): a
            # patient's token cannot reach a staff protocol however the question is put.
            hits = await self._index.search(
                request.query, audiences=audiences_for(caller.audience), limit=limit
            )
            logger.info("search", extra={"audience": caller.audience, "hits": len(hits)})
            return knowledge_pb2.SearchReply(hits=[
                knowledge_pb2.Hit(source_id=h.source_id, kind=h.kind, title=h.title,
                                  snippet=h.text[:400], score=h.score)
                for h in hits
            ])
        entries = await self._directory.search(request.query, limit)
        logger.info("search", extra={"audience": caller.audience, "hits": len(entries)})
        return knowledge_pb2.SearchReply(hits=[
            knowledge_pb2.Hit(
                source_id=f"provider:{e.provider_id}", kind="provider",
                title=e.name, snippet=e.specialty, score=1.0,
            )
            for e in entries
        ])


def build_server(directory: Directory, verifier: TokenVerifier, port: int,
                 index: Index | None = None) -> grpc.aio.Server:
    server = grpc.aio.server()
    knowledge_pb2_grpc.add_KnowledgeServicer_to_server(
        KnowledgeServicer(directory, verifier, index), server
    )
    checker = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(checker, server)
    server.port = server.add_insecure_port(f"[::]:{port}")  # the bound port (0 picks one)
    server.health = checker  # set SERVING by the process once its dependencies are up
    return server


SERVING = health_pb2.HealthCheckResponse.SERVING
