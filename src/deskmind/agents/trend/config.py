"""Versioned parameters of the trend agent. Any change here requires a new AGENT_VERSION."""

from __future__ import annotations

from dataclasses import asdict, dataclass

AGENT_NAME = "trend"
AGENT_VERSION = "1.2.0"  # 1.2.0: point-in-time storage (items + transitions), rules unchanged


@dataclass(frozen=True)
class TrendConfig:
    left_bars: int = 3
    right_bars: int = 3
    atr_period: int = 14
    min_swing_atr: float = 1.0
    trendline_tolerance_atr: float = 0.25
    trendline_min_touches: int = 3
    trendline_pivot_window: int = 8
    max_active_trendlines: int = 10
    trendline_max_idle_bars: int = 150  # no new touch for this many candles -> line expires
    range_after_bars: int = 100
    min_warmup_bars: int = 2  # refuse cold-start emission on synthetic single bar
    warmup_bars: int = 1000
    bar_memory: int = 5000  # candles kept in memory (trend-line close checks look back this far)
    version: str = AGENT_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_CONFIG = TrendConfig()
