"""Versioned parameters of the ICT Time agent.

Any change here requires a new AGENT_VERSION and an entry in docs/agents/ict_time.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

AGENT_NAME = "ict_time"
AGENT_VERSION = "1.0.0"

DayBoundary = Literal["ny_midnight", "utc_midnight"]


@dataclass(frozen=True)
class IctTimeConfig:
    # Day boundary for PDH/PDL (see spec §4.1)
    day_boundary: DayBoundary = "ny_midnight"

    # AMD heuristic
    amd_accumulation_atr_max: float = 0.5

    # Housekeeping
    min_warmup_bars: int = 2  # refuse cold-start emission on synthetic single bar
    warmup_bars: int = 1500  # enough to cover several days of 5m bars
    bar_memory: int = 3000
    version: str = AGENT_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_CONFIG = IctTimeConfig()
