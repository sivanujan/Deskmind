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
