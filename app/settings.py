"""Application settings.

Every externally-visible resource name (Kafka topic, RabbitMQ queue, Temporal task
queue) is built through this object so that a test run can namespace a shared
stack by setting a single prefix. Call sites must never hardcode a name.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    def topic(self, name: str) -> str:
        """Kafka topic name, namespaced by the active resource prefix."""
        return f"{self.resource_prefix}{name}"

    def queue(self, name: str) -> str:
        """RabbitMQ queue name, namespaced by the active resource prefix."""
        return f"{self.resource_prefix}{name}"

    def task_queue(self, name: str) -> str:
        """Temporal task queue name, namespaced by the active resource prefix."""
        return f"{self.resource_prefix}{name}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Call `get_settings.cache_clear()` in tests that patch env."""
    return Settings()
