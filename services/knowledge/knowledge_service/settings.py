"""The knowledge service's settings. Only its own stores -- no field here can point at
Part A's Postgres, Redis or Mongo (tests/meta/test_service_independence.py)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class KnowledgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_", extra="ignore")

    environment: str = "local"
    #: The service's own database (compose: knowledge-db). Its copy of the provider
    #: directory today; documents, versions and ingestion runs in Week 4.
    database_url: str = "postgresql+asyncpg://knowledge:knowledge@localhost:5442/knowledge"
    #: The vector database (Week 4); part of readiness from the start.
    qdrant_url: str = "http://localhost:6333"
    #: Where embeddings come from (harness requirement 2): fixture (the recorded vectors --
    #: every test), record (compute and save), live (compute).
    embedding_mode: str = "fixture"
    kafka_bootstrap_servers: str = "localhost:9092"
    schema_registry_url: str = "http://localhost:8081"
    #: Part A's topics carry its resource prefix; this names the same prefix.
    topic_prefix: str = ""
    kafka_group: str = "knowledge"
    #: Part A's published verification key (spec D-25): tokens are checked here, locally.
    jwks_url: str = "http://localhost:8000/.well-known/jwks.json"
    jwt_issuer: str = "smarthealth-part-a"
    grpc_port: int = 50052
    otel_endpoint_http: str = ""

    def topic(self, name: str) -> str:
        return f"{self.topic_prefix}{name}"
