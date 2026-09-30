"""Versioned parameters of the ICT/SMC liquidity agent.

Any change here requires a new AGENT_VERSION and an entry in docs/agents/ict_smc.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

AGENT_NAME = "ict_smc"
AGENT_VERSION = "1.2.0"


@dataclass(frozen=True)
class IctSmcConfig:
    # Liquidity pools (EQH/EQL and single swings)
    eq_tolerance_atr: float = 0.15
    pool_swing_lookback: int = 15
    max_active_pools: int = 10

    # Displacement
    displacement_min_atr: float = 1.0
    displacement_body_ratio: float = 0.6

    # Fair Value Gaps
    fvg_min_atr: float = 0.25
    max_active_fvgs: int = 10

    # Order Blocks & Breakers
    ob_max_candles: int = 3
    max_active_obs: int = 10
    max_active_breakers: int = 10

    # Liquidity voids
    void_min_atr: float = 1.5
    void_max_candles: int = 5
    max_active_voids: int = 5

    # Premium / Discount Range & OTE
    ote_low_pct: float = 0.62
    ote_high_pct: float = 0.79

    # SMT Divergence
    smt_pairs: tuple[tuple[str, str], ...] = (("BTCUSDT", "ETHUSDT"),)

    # Housekeeping & General
    zone_max_age_bars: int = 300
    warmup_bars: int = 1000
    bar_memory: int = 2000
    version: str = AGENT_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_CONFIG = IctSmcConfig()
