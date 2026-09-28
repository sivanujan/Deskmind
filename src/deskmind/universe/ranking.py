"""Liquidity-only universe ranking.

Membership = median daily quote volume (USDT) over the last `lookback_days` CLOSED daily candles.
Enter at rank <= top_n; an existing member stays while rank <= exit_rank (buffer so symbols do not
flip in and out every day). Volatility, trend clarity and volume spikes are NOT used here: they
belong to the Market Scanner, which picks today's opportunities inside the universe.

Public market data only (exchangeInfo, 24h tickers, daily klines) via the unauthenticated
RateLimitedClient, which enforces Binance request weights.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from typing import Any

from deskmind.universe.models import RankResult, SymbolStat

DAY_MS = 86_400_000


@dataclass(frozen=True)
class UniverseConfig:
    top_n: int = 30
    exit_rank: int = 40
    lookback_days: int = 30
    prefilter: int = 80  # symbols (by 24h quote volume) whose daily klines are fetched
    min_listing_age_days: int = 180
    history_days: int = 365  # history downloaded for an entering symbol (plus warm-up)
    warmup_days: int = 30
    download_concurrency: int = 2
    disk_max_used_pct: float = 90.0

    @classmethod
    def from_settings(cls, settings: Any) -> UniverseConfig:
        return cls(
            top_n=settings.universe_top_n,
            exit_rank=settings.universe_exit_rank,
            lookback_days=settings.universe_lookback_days,
            min_listing_age_days=settings.universe_min_listing_age_days,
            history_days=settings.universe_history_days,
            warmup_days=settings.universe_warmup_days,
            download_concurrency=settings.universe_download_concurrency,
            disk_max_used_pct=settings.universe_disk_max_used_pct,
        )

    def __post_init__(self) -> None:
        if not 0 < self.top_n <= self.exit_rank:
            raise ValueError("need 0 < top_n <= exit_rank")
        if self.download_concurrency < 1:
            raise ValueError("download_concurrency must be >= 1")


def rank_universe(
    stats: list[SymbolStat], previous_members: set[str], cfg: UniverseConfig
) -> RankResult:
    """Pure: rank by median quote volume (ties by symbol) and apply the exit buffer."""
    ordered = sorted(stats, key=lambda s: (-s.median_quote_volume, s.symbol))
    ranks = {s.symbol: i for i, s in enumerate(ordered, 1)}
    members = [
        s.symbol
        for s in ordered
        if ranks[s.symbol] <= cfg.top_n
        or (s.symbol in previous_members and ranks[s.symbol] <= cfg.exit_rank)
    ]
    entered = [s for s in members if s not in previous_members]
    exited = sorted(s for s in previous_members if s not in members)
    return RankResult(members=members, entered=entered, exited=exited, ranks=ranks)


async def fetch_liquidity_stats(
    client: Any, cfg: UniverseConfig, now_ms: int, include: set[str]
) -> list[SymbolStat]:
    """USDT-margined perpetuals in TRADING status, with their median daily quote volume.

    `include` (current members) is always measured, so a member outside today's prefilter still
    gets a real rank instead of silently dropping out.
    """
    info = await client.get_json("/fapi/v1/exchangeInfo", weight=1)
    listed = {
        s["symbol"]: int(s.get("onboardDate") or 0)
        for s in info.get("symbols", [])
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }
    tickers = await client.get_json("/fapi/v1/ticker/24hr", weight=40)
    by_24h = sorted(
        (t for t in tickers if t.get("symbol") in listed),
        key=lambda t: -float(t.get("quoteVolume") or 0),
    )
    candidates = {t["symbol"] for t in by_24h[: cfg.prefilter]} | (include & listed.keys())

    today_open = now_ms - now_ms % DAY_MS

    async def one(sym: str) -> SymbolStat | None:
        rows = await client.get_json(
            "/fapi/v1/klines",
            params={"symbol": sym, "interval": "1d", "limit": cfg.lookback_days + 1},
            weight=1,
        )
        closed = [r for r in rows if int(r[0]) < today_open][-cfg.lookback_days :]
        if not closed:
            return None
        vols = [float(r[7]) for r in closed]  # index 7 = quote asset volume
        return SymbolStat(sym, listed[sym], statistics.median(vols), len(closed))

    results = await asyncio.gather(*(one(s) for s in sorted(candidates)))
    return [r for r in results if r is not None]
