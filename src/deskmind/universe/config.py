"""Universe parameters, read from Settings (.env: UNIVERSE_*)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from deskmind.agents.trend.config import DEFAULT_CONFIG as TREND_CONFIG
from deskmind.config import Settings
from deskmind.data.datasets import DAY_MS, TIMEFRAME_MS

RUN_HOUR_UTC = 0  # the daily job runs at 00:00 UTC, right after the daily candle closes


def agent_warmup_days(timeframes: list[str]) -> int:
    """Days of history the trend agent replays before its first report (largest timeframe)."""
    widest = max(TIMEFRAME_MS[tf] for tf in timeframes)
    return math.ceil(TREND_CONFIG.warmup_bars * widest / DAY_MS)


@dataclass(frozen=True)
class UniverseConfig:
    top_n: int
    min_quote_volume_usdt: Decimal  # 24h quote volume floor (liquidity)
    exit_rank_buffer: int  # a member only exits once its rank falls below top_n + buffer
    min_listing_age_days: int  # younger listings are watch_only: data + chart, never traded
    history_days: int  # history needed for a new symbol, on top of the agent warm-up
    warmup_days: int
    download_concurrency: int
    disk_max_used_pct: Decimal  # stop downloading and alert above this disk usage
    run_hour_utc: int = RUN_HOUR_UTC

    @classmethod
    def from_settings(cls, settings: Settings) -> UniverseConfig:
        return cls(
            top_n=settings.universe_top_n,
            min_quote_volume_usdt=settings.universe_min_quote_volume_usdt,
            exit_rank_buffer=settings.universe_exit_rank_buffer,
            min_listing_age_days=settings.universe_min_listing_age_days,
            history_days=settings.universe_history_days,
            warmup_days=agent_warmup_days(list(settings.live_timeframes)),
            download_concurrency=settings.universe_download_concurrency,
            disk_max_used_pct=settings.universe_disk_max_used_pct,
        )

    def as_dict(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}
