"""Redis client and key namespacing."""

from __future__ import annotations

from redis.asyncio import Redis

from app.settings import Settings


def create_redis_client(settings: Settings) -> Redis:
    """Constructs a client and a pool without connecting."""
    return Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
    )


def namespaced_key(settings: Settings, key: str) -> str:
    """Prefix a key with this run's namespace.

    Redis has no schemas, so the prefix is the only thing keeping two concurrent test
    runs against one server from reading each other's state.
    """
    return f"{settings.redis_prefix}{key}"
