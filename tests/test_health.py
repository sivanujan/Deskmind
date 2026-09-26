"""Integration tests: require `make up` + `make migrate`. Skipped if services are unreachable."""

import socket

import pytest
from sqlalchemy import text

from deskmind.config import get_settings
from deskmind.db.session import create_engine
from deskmind.health import check_config, check_postgres, check_redis, run_all


def _reachable(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


settings = get_settings()
needs_pg = pytest.mark.skipif(
    not _reachable(settings.postgres_host_port), reason="deskmind postgres not running"
)
needs_redis = pytest.mark.skipif(
    not _reachable(settings.redis_host_port), reason="deskmind redis not running"
)


def test_config_ok() -> None:
    result, s = check_config()
    assert result.ok, result.detail
    assert s is not None and s.postgres_host_port != 5432


@needs_pg
async def test_postgres_ok() -> None:
    result = await check_postgres(settings)
    assert result.ok, result.detail


@needs_redis
async def test_redis_ok() -> None:
    result = await check_redis(settings)
    assert result.ok, result.detail


@needs_pg
@needs_redis
async def test_run_all_ok() -> None:
    assert all(r.ok for r in await run_all() if r.required)


@needs_pg
async def test_schema_migrated() -> None:
    engine = create_engine(settings)
    try:
        async with engine.connect() as conn:
            exts = set(
                (await conn.execute(text("SELECT extname FROM pg_extension"))).scalars().all()
            )
            hypertables = set(
                (
                    await conn.execute(
                        text("SELECT hypertable_name FROM timescaledb_information.hypertables")
                    )
                )
                .scalars()
                .all()
            )
            tables = set(
                (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    assert {"timescaledb", "vector"} <= exts
    assert "candles" in hypertables
    assert {"candles", "agent_reports", "trade_journal", "strategy_library", "lessons"} <= tables
