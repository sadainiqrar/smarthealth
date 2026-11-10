"""The assistant service's settings. Its own Redis and nothing of Part A's stores
(tests/meta/test_service_independence.py); Part A is reached over its HTTP API only."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class AssistantSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASSISTANT_", extra="ignore")

    environment: str = "local"
    #: The service's own Redis (compose: assistant-redis): turn streams, the revoked-session
    #: deny-list, and in Week 5 conversation checkpoints and the LLM cache.
    redis_url: str = "redis://localhost:6390/0"
    #: Where the knowledge service listens (native gRPC, no Envoy in between).
    knowledge_target: str = "localhost:50052"
    kafka_bootstrap_servers: str = "localhost:9092"
    schema_registry_url: str = "http://localhost:8081"
    topic_prefix: str = ""
    kafka_group: str = "assistant"
    jwks_url: str = "http://localhost:8000/.well-known/jwks.json"
    jwt_issuer: str = "smarthealth-part-a"
    grpc_port: int = 50051
    #: Turn streams are kept this long for a client to resume.
    turn_ttl_seconds: int = 3600
    #: Fixture until Week 5 brings a key (spec O-9): nothing here may need a live LLM.
    llm_mode: str = "fixture"
    otel_endpoint_http: str = ""

    def topic(self, name: str) -> str:
        return f"{self.topic_prefix}{name}"
