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
    #: auto | off | fixture | record | live (llm.py). `auto` is live when a key is set and
    #: fixture otherwise; a request with no recording falls back to the offline brain.
    llm_mode: str = "auto"
    #: groq | anthropic | openai -- used by record and live only. Switching provider is
    #: these three settings; empty model means the provider's default (llm.DEFAULT_MODELS).
    llm_provider: str = "groq"
    llm_model: str = ""
    llm_api_key: str = ""
    #: Part A's HTTP API, called with the user's own token (parta.py).
    parta_url: str = "http://localhost:8000"
    parta_timeout_seconds: float = 5.0
    #: Conversation checkpoints (LangGraph): the service's own Postgres (compose:
    #: assistant-db). Empty keeps them in memory -- tests, and a single dev process.
    database_url: str = ""
    #: Times are said to users in the clinic's zone.
    display_timezone: str = "UTC"
    otel_endpoint_http: str = ""

    def topic(self, name: str) -> str:
        return f"{self.topic_prefix}{name}"
