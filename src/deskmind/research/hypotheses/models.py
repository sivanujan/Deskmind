"""Data models for research strategy hypothesis signals and evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Signal:
    hypothesis_id: str  # e.g., "H1_v1.0.0"
    symbol: str
    timestamp: int      # 5m bar close timestamp when signal was evaluated
    direction: str      # "bullish" | "bearish"
    entry_price: float
    stop_price: float
    target_price: float
    r_multiple: float   # Target R distance ratio (target_dist / stop_dist)
    rationale: str
    setup_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_setup_id(hypothesis_code: str, symbol: str, anchor_id_or_time: Any, direction: str) -> str:
    """Canonical setup_id formula shared by all hypotheses and backtest strategies:

    {symbol}:{hypothesis_code}:{anchor_id_or_time}:{direction}
    Ensures exact 1-to-1 setup-level deduplication between research funnel and backtest engine.
    """
    return f"{symbol}:{hypothesis_code}:{anchor_id_or_time}:{direction}"
