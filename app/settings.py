"""Application settings.

Every externally-visible resource name (Kafka topic, RabbitMQ queue, Temporal task
queue) is built through this object so that a test run can namespace a shared
stack by setting a single prefix. Call sites must never hardcode a name.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

LlmMode = Literal["fixture", "record", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTHEALTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "local"
    service_name: str = "smarthealth"

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

    # --- Auth ---
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60

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
        """user:password@host:port, with credentials percent-encoded.

        An unencoded `@` or `/` in a password silently corrupts the URL's authority
        section, producing a confusing connection failure rather than a clear one.
        """
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return f"{user}:{password}@{self.postgres_host}:{self.postgres_port}"

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
        return f"mongodb://{self.mongo_host}:{self.mongo_port}"

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Call `get_settings.cache_clear()` in tests that patch env."""
    return Settings()
