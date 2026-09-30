"""Versioned parameters of the Price Action agent.

Any change here requires a new AGENT_VERSION and doc update (docs/agents/price_action.md).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

AGENT_NAME = "price_action"
AGENT_VERSION = "1.0.0"


@dataclass(frozen=True)
class PriceActionConfig:
    # Support / Resistance zones
    sr_cluster_atr: float = 0.3      # swing proximity to cluster (x ATR)
    sr_zone_width_atr: float = 0.15  # minimum zone half-width (x ATR)
    sr_min_touches: int = 2          # minimum touches to form a zone
    sr_max_age_bars: int = 500       # retire zones with no interaction after N bars
    zone_tag_atr: float = 0.5        # max distance to tag a pattern as "at_zone" (x ATR)

    # Breakout / retest
    bo_min_atr: float = 1.0          # minimum close distance from zone edge (x ATR)
    bo_retest_max_bars: int = 20     # maximum bars to wait for a retest

    # Candle patterns
    pin_wick_ratio: float = 2.0      # min wick/body for hammer / shooting star
    doji_body_atr: float = 0.1       # max body for doji (x ATR)

    # Range detection
    range_min_bars: int = 5
    range_max_bars: int = 50
    range_width_atr: float = 1.5

    # Round numbers: determined automatically per symbol if 0
    round_number_step: float = 0.0

    # Housekeeping
    min_warmup_bars: int = 200        # justified: 14-period ATR + S/R swing cluster formation lookback
    warmup_bars: int = 1000          # bars to replay on warm-up
    bar_memory: int = 2000
    atr_period: int = 14

    version: str = AGENT_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_CONFIG = PriceActionConfig()

# Auto round-number steps per symbol pattern (matched via substring)
ROUND_NUMBER_STEPS: dict[str, float] = {
    "BTC": 1000.0,
    "ETH": 100.0,
    "SOL": 10.0,
    "BNB": 10.0,
    "XRP": 0.1,
}
