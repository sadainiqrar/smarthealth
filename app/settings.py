"""Application settings.

Every externally-visible resource name (Kafka topic, RabbitMQ queue, Temporal task
queue) is built through this object so that a test run can namespace a shared
stack by setting a single prefix. Call sites must never hardcode a name.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmMode = Literal["fixture", "record", "live"]

#: Development-only Ed25519 signing key. Public in the repository, so it signs nothing
#: that matters: `app.main` refuses it in production (see `jwt_private_key`).
DEV_SIGNING_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MC4CAQAwBQYDK2VwBCIEIJBtwyV+dtqRwEGEfHDO2V04AQNLbrJqUX76MxgPRuE3\n"
    "-----END PRIVATE KEY-----\n"
)


def _encode_credential(value: str) -> str:
    """Percent-encode a URI userinfo component.

    `quote_plus` is wrong here: it encodes a space as `+`, which is a query-string
    convention. URI parsers unquote only `%XX`, so the `+` survives literally and the
    credential silently becomes a different string. `quote(safe="")` encodes a space
    as `%20`, which round-trips correctly.
    """
    return quote(value, safe="")


def _format_host(host: str) -> str:
    """Bracket an IPv6 literal.

    RFC 3986 requires it: unbracketed, the colons in `::1` are ambiguous with the
    port separator and the authority will not parse.
    """
    return f"[{host}]" if ":" in host else host


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTHEALTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "local"
    service_name: str = "smarthealth"

    # Raising verbosity during an incident should be an environment variable and a
    # restart, not a code change and a redeploy.
    log_level: str = "INFO"

    # --- OpenTelemetry (Week 3, spec D-28) ---
    # Empty means "build the providers, export nowhere": tests and a bare laptop run with
    # no collector and no connection errors. Compose points both at the collector.
    otel_endpoint_http: str = ""  # OTLP/HTTP base URL, e.g. http://otel-collector:4318
    otel_endpoint_grpc: str = ""  # OTLP/gRPC URL, for Temporal core's own SDK metrics

    # Namespaces every Kafka topic, RabbitMQ queue, and Temporal task queue.
    # The isolation layer sets this per test run; production leaves it empty.
    resource_prefix: str = ""

    # Selects the LLM/embedding provider implementation (spec section 9).
    llm_mode: LlmMode = "fixture"

    # --- PostgreSQL: the system of record ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "smarthealth"
    postgres_password: str = "smarthealth"
    postgres_db: str = "smarthealth"

    # --- MongoDB: the audit trail (design spec section 3.4) ---
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_db: str = "smarthealth"

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_prefix: str = ""

    # --- Kafka + Schema Registry: domain events (Week 3 slice 3, spec section 3) ---
    kafka_bootstrap_servers: str = "localhost:9092"
    schema_registry_url: str = "http://localhost:8081"
    #: Consumer-group prefix. The isolation layer gives each test run its own, so two runs
    #: on one broker never steal each other's partitions.
    kafka_group: str = "smarthealth"

    # --- Temporal: durable workflows (PRD D-7) ---
    temporal_host: str = "localhost"
    temporal_port: int = 7233
    temporal_namespace: str = "default"
    #: Minutes after a slot's end before an appointment nobody checked in for becomes a
    #: no-show (spec 4.2). Read by the worker, which hands it to the visit activities.
    #: Zero is allowed (no grace); a negative grace would mark patients no-show before
    #: their slot ended, so it is refused at startup.
    no_show_grace_minutes: int = Field(default=15, ge=0)

    # --- Auth (Week 3 slice 2; Weeks 3-5 spec D-25) ---
    # Ed25519 private key, PEM. Asymmetric so the Part B services verify tokens with the
    # public half (GET /.well-known/jwks.json) and never hold anything that can sign one.
    # The default is a DEVELOPMENT key, committed on purpose like the HS256 secret it
    # replaces; the API refuses to start with it when `environment` is "production".
    # `python -m app.cli generate-signing-key` prints a fresh one.
    jwt_private_key: str = DEV_SIGNING_KEY
    jwt_issuer: str = "smarthealth-part-a"
    #: Short, because revocation acts on the session, and a token whose session is gone is
    #: refused by Part A at once -- but the Part B services verify the signature alone, so
    #: this is their worst-case revocation lag if the revocation event never arrives.
    access_token_minutes: int = Field(default=10, ge=1)
    #: Absolute: refreshing does not extend a session.
    session_hours: int = Field(default=12, ge=1)
    # Login throttling (PART-A-NFR-8): per address, and a looser one per client IP.
    login_max_failures: int = Field(default=5, ge=1)
    login_ip_max_failures: int = Field(default=50, ge=1)
    login_window_minutes: int = Field(default=15, ge=1)
    #: Per user, on POST /appointments and POST /appointments/{id}/reschedule.
    booking_rate_limit_per_minute: int = Field(default=10, ge=1)

    @field_validator("jwt_private_key", mode="before")
    @classmethod
    def _empty_key_means_the_development_key(cls, value: object) -> object:
        """Compose passes `SMARTHEALTH_JWT_PRIVATE_KEY=` (empty) when nothing is set, and
        an empty string must mean "not configured", not "a key that fails to parse"."""
        return DEV_SIGNING_KEY if value in ("", None) else value

    def topic(self, name: str) -> str:
        """Kafka topic name, namespaced by the active resource prefix."""
        return f"{self.resource_prefix}{name}"

    def queue(self, name: str) -> str:
        """RabbitMQ queue name, namespaced by the active resource prefix."""
        return f"{self.resource_prefix}{name}"

    def task_queue(self, name: str) -> str:
        """Temporal task queue name, namespaced by the active resource prefix."""
        return f"{self.resource_prefix}{name}"

    @property
    def _postgres_authority(self) -> str:
        """user:password@host:port, with credentials percent-encoded."""
        user = _encode_credential(self.postgres_user)
        password = _encode_credential(self.postgres_password)
        return f"{user}:{password}@{_format_host(self.postgres_host)}:{self.postgres_port}"

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy async URL for the application database."""
        return f"postgresql+asyncpg://{self._postgres_authority}/{self.postgres_db}"

    @property
    def postgres_admin_url(self) -> str:
        """Plain libpq URL to the maintenance database.

        Used by the test harness to CREATE/DROP a per-run database, which cannot be
        done from a connection to that database. No `+asyncpg` — asyncpg.connect
        rejects the SQLAlchemy dialect prefix.
        """
        return f"postgresql://{self._postgres_authority}/postgres"

    @property
    def mongo_uri(self) -> str:
        return f"mongodb://{_format_host(self.mongo_host)}:{self.mongo_port}"

    @property
    def redis_url(self) -> str:
        return f"redis://{_format_host(self.redis_host)}:{self.redis_port}/{self.redis_db}"

    @property
    def temporal_address(self) -> str:
        """host:port for the Temporal frontend. No scheme: the SDK takes a bare target."""
        return f"{_format_host(self.temporal_host)}:{self.temporal_port}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Call `get_settings.cache_clear()` in tests that patch env."""
    return Settings()
