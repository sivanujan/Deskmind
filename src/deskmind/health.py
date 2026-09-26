"""Health checks for config, Postgres and Redis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import SecretStr, ValidationError
from sqlalchemy import text

from deskmind.cache.redis_client import create_redis
from deskmind.config import Settings
from deskmind.db.session import create_engine

CHECK_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True  # False: shown as WARN, does not fail `deskmind health`


def check_config() -> tuple[CheckResult, Settings | None]:
    try:
        settings = Settings()
    except ValidationError as exc:
        msgs = "; ".join(e["msg"] for e in exc.errors())
        return CheckResult("config", False, msgs), None

    def is_set(secret: SecretStr) -> str:
        return "set" if secret.get_secret_value() else "missing"

    detail = (
        f"TRADING_MODE={settings.trading_mode}; "
        f"binance={settings.binance_futures_base_url} "
        f"(key {is_set(settings.binance_api_key)}, secret {is_set(settings.binance_secret_key)}); "
        f"llm=openrouter:{settings.openrouter_model} (key {is_set(settings.openrouter_api_key)})"
    )
    if not settings.postgres_password.get_secret_value():
        return CheckResult("config", False, f"{detail}; POSTGRES_PASSWORD is empty"), settings
    return CheckResult("config", True, detail), settings


async def check_postgres(settings: Settings) -> CheckResult:
    name = f"postgres ({settings.postgres_host}:{settings.postgres_host_port})"
    engine = create_engine(settings)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S), engine.connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar_one()
            ext_rows = await conn.execute(
                text(
                    "SELECT extname, extversion FROM pg_extension "
                    "WHERE extname IN ('timescaledb', 'vector') ORDER BY extname"
                )
            )
            exts = ", ".join(f"{n} {v}" for n, v in ext_rows.all()) or "no extensions yet"
        return CheckResult(name, True, f"PostgreSQL {version}; {exts}")
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()


async def check_redis(settings: Settings) -> CheckResult:
    name = f"redis ({settings.redis_host}:{settings.redis_host_port})"
    client = create_redis(settings)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await client.ping()
            info = await client.info("server")
        return CheckResult(name, True, f"Redis {info.get('redis_version', '?')}")
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")
    finally:
        await client.aclose()


async def run_all() -> list[CheckResult]:
    cfg, settings = check_config()
    if settings is None:
        return [cfg]
    pg, rd = await asyncio.gather(check_postgres(settings), check_redis(settings))
    results = [cfg, pg, rd]
    if rd.ok:
        results.append(await check_live(settings))
    return results


async def check_live(settings: Settings) -> CheckResult:
    from deskmind.data.live.commands import live_health

    try:
        ok, detail = await live_health(settings)
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return CheckResult("live service", ok, detail, required=False)
