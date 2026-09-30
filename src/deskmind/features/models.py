"""Feature row data structures and schema definitions for point-in-time feature store."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class FeatureRow:
    timestamp: int
    symbol: str
    htf_trend_2h: str
    htf_trend_1h: str
    last_event_15m: str
    last_event_15m_confirmed_at: int | None
    last_event_5m: str
    last_event_5m_confirmed_at: int | None
    pd_zone_1h: str
    position_pct_1h: float | None
    outside_range_1h: bool
    in_ote_1h: bool
    nearest_fvg_size_atr: float
    nearest_fvg_displacement: bool
    nearest_fvg_dist_atr: float
    nearest_ob_state: str
    nearest_ob_dist_atr: float
    strong_pool_swept_side: str
    strong_pool_swept_bars_ago: int
    killzone: str
    silver_bullet_active: bool
    session: str
    dist_to_pdh_atr: float | None
    dist_to_pdl_atr: float | None
    pdh_swept_recent: bool
    pdl_swept_recent: bool
    nearest_sr_strength: int
    nearest_sr_dist_atr: float
    pattern_recent_type: str
    pattern_recent_at_zone: bool
    pattern_recent_with_trend: str
    pattern_recent_bars_ago: int
    weekend_flag: bool
    atr_pct: float
    universe_member: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

