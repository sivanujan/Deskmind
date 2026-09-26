"""Async Redis client."""

from __future__ import annotations

from redis.asyncio import Redis

from deskmind.config import Settings, get_settings


def create_redis(settings: Settings | None = None) -> Redis:
    settings = settings or get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=True)
