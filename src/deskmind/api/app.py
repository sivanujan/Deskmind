"""Read-only HTTP/WebSocket API for the web app (GET endpoints + one chart WebSocket).

There is deliberately no endpoint that can place, modify or cancel orders, and no write to
Postgres, Redis or Parquet. Bound to 127.0.0.1 only (see `deskmind api`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from deskmind.agents.trend.config import AGENT_VERSION
from deskmind.agents.trend.overlays import to_overlays
from deskmind.agents.trend.store import TrendStore
from deskmind.api.candles import MAX_LIMIT, CandleService, PostgresRecentCandles
from deskmind.api.live import live_status, stream_chart
from deskmind.api.overlays import OverlayResponse
from deskmind.api.schemas import (
    CandlesResponse,
    HealthCheck,
    HealthResponse,
    LiveStatusResponse,
    SymbolsResponse,
    TrendReportResponse,
)
from deskmind.config import Settings, get_settings
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B

API_VERSION = "0.1.0"


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    candles: CandleService | None = None,
    trend_store: TrendStore | None = None,
    health_checks: bool = True,
) -> FastAPI:
    """App factory. Tests inject fake Redis/candle sources; production builds real ones."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if redis is None:
            from deskmind.cache.redis_client import create_redis

            app.state.redis = create_redis(settings)
        if candles is None:
            from deskmind.db.session import create_engine

            engine = create_engine(settings)
            app.state.candles = CandleService(settings.data_dir, PostgresRecentCandles(engine))
        if trend_store is None:
            from deskmind.agents.trend.store import PostgresTrendStore
            from deskmind.db.session import create_engine

            engine = engine or create_engine(settings)
            app.state.trend_store = PostgresTrendStore(engine)
        yield
        if redis is None:
            await app.state.redis.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="Deskmind read-only API",
        version=API_VERSION,
        description="Market data for the Deskmind web app. Read-only: no trading endpoints.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    if redis is not None:
        app.state.redis = redis
    if candles is not None:
        app.state.candles = candles
    if trend_store is not None:
        app.state.trend_store = trend_store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.web_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    symbols = list(settings.data_symbols)
    timeframes = list(settings.live_timeframes)

    def check(symbol: str, tf: str) -> str:
        sym = symbol.upper()
        if sym not in symbols:
            raise HTTPException(404, f"unknown symbol {symbol}; configured: {symbols}")
        if tf not in timeframes:
            raise HTTPException(404, f"unknown timeframe {tf}; available: {timeframes}")
        return sym

    @app.get("/api/symbols", response_model=SymbolsResponse)
    async def get_symbols() -> SymbolsResponse:
        return SymbolsResponse(symbols=symbols, timeframes=timeframes)

    @app.get("/api/candles", response_model=CandlesResponse)
    async def get_candles(
        symbol: str,
        tf: str,
        start: Annotated[int | None, Query(description="UNIX seconds, inclusive")] = None,
        end: Annotated[int | None, Query(description="UNIX seconds, exclusive")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 500,
    ) -> CandlesResponse:
        sym = check(symbol, tf)
        rows = await app.state.candles.candles(
            sym,
            tf,
            start_ms=start * 1000 if start is not None else None,
            end_ms=end * 1000 if end is not None else None,
            limit=limit,
            now_ms=B.now_ms(),
        )
        return CandlesResponse(symbol=sym, timeframe=tf, count=len(rows), candles=rows)

    @app.get("/api/live/status", response_model=LiveStatusResponse)
    async def get_live_status() -> LiveStatusResponse:
        return await live_status(app.state.redis, symbols, timeframes)

    @app.get("/api/health", response_model=HealthResponse)
    async def get_health() -> HealthResponse:
        checks: list[HealthCheck] = []
        if health_checks:
            from deskmind.health import run_all

            checks = [
                HealthCheck(name=r.name, ok=r.ok, required=r.required, detail=r.detail)
                for r in await run_all()
            ]
        live = await live_status(app.state.redis, symbols, timeframes)
        return HealthResponse(ok=all(c.ok for c in checks if c.required), checks=checks, live=live)

    @app.get("/api/overlays", response_model=OverlayResponse)
    async def get_overlays(
        symbol: str,
        tf: str,
        start: Annotated[int | None, Query(description="UNIX seconds")] = None,
        end: Annotated[int | None, Query(description="UNIX seconds")] = None,
        as_of: Annotated[
            int | None,
            Query(
                description="Replay: only what was known at this UNIX second (confirmed_at <= T)"
            ),
        ] = None,
        run_id: Annotated[
            str | None,
            Query(description="Backtest run ID to overlay trades for"),
        ] = None,
    ) -> OverlayResponse:
        sym = check(symbol, tf)
        iv = TIMEFRAME_MS[tf]
        t = as_of * 1000 if as_of is not None else B.now_ms()
        view_end = (t // iv) * iv - iv
        overlays = []
        if getattr(app.state, "trend_store", None) is not None:
            items = await app.state.trend_store.items(
                sym,
                tf,
                start * 1000 if start is not None else None,
                min(end * 1000, view_end) if end is not None else view_end,
                as_of * 1000 if as_of is not None else None,
            )
            overlays = to_overlays(items, AGENT_VERSION, view_end)

        if run_id:
            from deskmind.api.overlays import load_backtest_overlays

            overlays.extend(load_backtest_overlays(run_id, sym, start, end, as_of))
        return OverlayResponse(symbol=sym, timeframe=tf, overlays=overlays)

    @app.get("/api/backtest/runs")
    async def get_backtest_runs() -> list[dict[str, Any]]:
        from deskmind.backtest.registry import list_runs_local

        return list_runs_local()

    @app.get("/api/agents/trend/report", response_model=TrendReportResponse)
    async def get_trend_report(
        symbol: str,
        as_of: Annotated[
            int | None, Query(description="UNIX seconds; latest report at or before it")
        ] = None,
    ) -> TrendReportResponse:
        sym = symbol.upper()
        if sym not in symbols:
            raise HTTPException(404, f"unknown symbol {symbol}")
        report = await app.state.trend_store.latest_report(
            sym, as_of * 1000 if as_of is not None else None
        )
        if report is None:
            raise HTTPException(404, "no trend report stored for this time (run the backfill)")
        return TrendReportResponse(**report)

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket, symbol: str, tf: str) -> None:
        origin = websocket.headers.get("origin")
        if origin and origin not in settings.web_origins:
            await websocket.close(code=1008)
            return
        try:
            sym = check(symbol, tf)
        except HTTPException:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await stream_chart(websocket, app.state.redis, sym, tf)

    return app
