"""Response models of the read-only API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SymbolEntry(BaseModel):
    symbol: str
    state: str | None  # universe lifecycle state; None = configured only (DATA_SYMBOLS)
    configured: bool  # in DATA_SYMBOLS
    has_data: bool  # history on disk (Parquet)


class SymbolsResponse(BaseModel):
    symbols: list[str]
    timeframes: list[str]
    entries: list[SymbolEntry] = []


class CandleOut(BaseModel):
    """A closed candle for display. Floats: charting only, never for money logic."""

    time: int = Field(description="Open time, UNIX seconds (UTC)")
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: Literal["parquet", "postgres", "redis"] = "parquet"


class CandlesResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    candles: list[CandleOut]


class TimeframeStatus(BaseModel):
    timeframe: str
    last_closed_open_time: int | None = Field(None, description="UNIX seconds")
    buffer_size: int = 0
    stream_age_s: float | None = None
    open_candle_age_s: float | None = None
    behind_candles: int | None = Field(None, description="Closed candles not yet received")
    stale: bool = False


class SymbolStatus(BaseModel):
    symbol: str
    timeframes: list[TimeframeStatus]
    mark_price: str | None = None
    funding_rate: str | None = None
    next_funding_time: int | None = Field(None, description="UNIX seconds")
    mark_age_s: float | None = None
    open_interest: str | None = None
    open_interest_age_s: float | None = None
    last_bar_close: int | None = Field(None, description="UNIX seconds")


class LiveStatusResponse(BaseModel):
    level: Literal["green", "amber", "red"]
    reasons: list[str]
    running: bool
    state: str | None = None
    ws_state: str | None = None
    heartbeat_age_s: float | None = None
    reconnects: int | None = None
    clock_drift_ms: int | None = None
    stale_streams: list[str] = Field(default_factory=list)
    symbols: list[SymbolStatus] = Field(default_factory=list)


class HealthCheck(BaseModel):
    name: str
    ok: bool
    required: bool
    detail: str


class HealthResponse(BaseModel):
    ok: bool = Field(description="All required checks pass")
    checks: list[HealthCheck]
    live: LiveStatusResponse


class TrendReportResponse(BaseModel):
    """Trend agent report as of a bar close (see docs/agents/trend.md §7)."""

    agent: str
    agent_version: str
    symbol: str
    as_of: str
    as_of_ms: int
    timeframes: dict[str, dict[str, Any]]
