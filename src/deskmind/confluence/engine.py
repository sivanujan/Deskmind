"""Deterministic Multi-Timeframe (MTF) alignment and equal-weight confluence score engine."""

from __future__ import annotations

from typing import Any

from deskmind.confluence.models import ConfluenceResult
from deskmind.features.models import FeatureRow


def evaluate_confluence(feature_input: FeatureRow | dict[str, Any]) -> ConfluenceResult:
    """Evaluate MTF alignment and equal-weight transparent confluence score.

    Timeframe cascade:
      Bias: 2h / 1h trend
      Setup: 15m structure break / zone
      Trigger: 5m structure break / pattern

    Alignment output:
      - 'aligned': HTF bias, 15m setup, and 5m trigger agree in direction
      - 'partial': 2 out of 3 levels agree (e.g. 1h bias agrees with 5m trigger, 15m neutral)
      - 'conflict': HTF bias opposes 5m trigger (e.g. 1h bullish vs 5m bearish CHoCH)
    """
    row = feature_input.as_dict() if isinstance(feature_input, FeatureRow) else feature_input

    # 1. Determine Bias (HTF: 2h / 1h)
    t_2h = row.get("htf_trend_2h", "ranging")
    t_1h = row.get("htf_trend_1h", "ranging")

    if t_2h == t_1h and t_1h in ("bullish", "bearish"):
        bias = t_1h
    elif t_1h in ("bullish", "bearish"):
        bias = t_1h
    elif t_2h in ("bullish", "bearish"):
        bias = t_2h
    else:
        bias = "neutral"

    # 2. Determine Setup Direction (15m)
    ev_15m = str(row.get("last_event_15m", "none"))
    if "up" in ev_15m:
        setup_dir = "bullish"
    elif "down" in ev_15m:
        setup_dir = "bearish"
    else:
        setup_dir = "neutral"

    # 3. Determine Trigger Direction (5m)
    ev_5m = str(row.get("last_event_5m", "none"))
    if "up" in ev_5m:
        trigger_dir = "bullish"
    elif "down" in ev_5m:
        trigger_dir = "bearish"
    else:
        trigger_dir = "neutral"

    # 4. Determine Alignment Status
    if bias != "neutral" and setup_dir == bias and trigger_dir == bias:
        alignment = "aligned"
    elif bias != "neutral" and (trigger_dir == bias or setup_dir == bias) and trigger_dir != ("bearish" if bias == "bullish" else "bullish"):
        alignment = "partial"
    elif bias != "neutral" and trigger_dir != "neutral" and trigger_dir != bias:
        alignment = "conflict"
    else:
        alignment = "partial" if (setup_dir == trigger_dir and trigger_dir != "neutral") else "conflict"

    # 5. Equal-Weight Components (10 components x 1.0 = Max 10.0)
    components: dict[str, float] = {}

    # C1: HTF Trend Agreement
    components["htf_trend_aligned"] = 1.0 if t_2h == t_1h and t_1h != "ranging" else 0.0

    # C2: P/D Zone Confluence
    pd_zone = row.get("pd_zone_1h", "none")
    if (bias == "bullish" and pd_zone == "discount") or (bias == "bearish" and pd_zone == "premium"):
        components["pd_zone_aligned"] = 1.0
    else:
        components["pd_zone_aligned"] = 0.0

    # C3: OTE Active
    components["ote_active"] = 1.0 if bool(row.get("in_ote_1h", False)) else 0.0

    # C4: Fresh Order Block Nearby (<= 1.0 ATR)
    ob_dist = float(row.get("nearest_ob_dist_atr", 999.0))
    ob_state = str(row.get("nearest_ob_state", "none"))
    components["nearest_ob_confluence"] = 1.0 if (ob_dist <= 1.0 and ob_state == "fresh") else 0.0

    # C5: Active FVG Nearby (<= 1.0 ATR, size >= 0.25 ATR)
    fvg_dist = float(row.get("nearest_fvg_dist_atr", 999.0))
    fvg_size = float(row.get("nearest_fvg_size_atr", 0.0))
    components["nearest_fvg_confluence"] = 1.0 if (fvg_dist <= 1.0 and fvg_size >= 0.25) else 0.0

    # C6: Recent Strong Pool Sweep
    sweep_side = str(row.get("strong_pool_swept_side", "none"))
    sweep_bars = int(row.get("strong_pool_swept_bars_ago", -1))
    if (bias == "bullish" and sweep_side == "SSL" and 0 <= sweep_bars <= 24) or (
        bias == "bearish" and sweep_side == "BSL" and 0 <= sweep_bars <= 24
    ):
        components["recent_liquidity_sweep"] = 1.0
    else:
        components["recent_liquidity_sweep"] = 0.0

    # C7: Time Window (Killzone / Silver Bullet)
    kz = str(row.get("killzone", "none"))
    sb = bool(row.get("silver_bullet_active", False))
    components["time_window_active"] = 1.0 if (kz != "none" or sb) else 0.0

    # C8: 5m Structure Trigger
    components["trigger_active"] = 1.0 if (trigger_dir == bias and bias != "neutral") else 0.0

    # C9: PDH/PDL Sweep Recent
    pdh_sw = bool(row.get("pdh_swept_recent", False))
    pdl_sw = bool(row.get("pdl_swept_recent", False))
    components["pd_level_swept"] = 1.0 if (pdh_sw or pdl_sw) else 0.0

    # C10: Price Action S/R & Pattern
    sr_str = int(row.get("nearest_sr_strength", 0))
    at_zone = bool(row.get("pattern_recent_at_zone", False))
    components["pa_sr_pattern"] = 1.0 if (sr_str >= 2 and at_zone) else 0.0

    total_score = sum(components.values())

    return ConfluenceResult(
        alignment=alignment,
        bias=bias,
        score=total_score,
        max_score=10.0,
        components=components,
    )
