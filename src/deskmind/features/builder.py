"""Feature row builder from point-in-time agent states."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from deskmind.features.models import FeatureRow


def build_feature_row(
    symbol: str,
    timestamp: int,
    trend_state_2h: dict[str, Any] | None,
    trend_state_1h: dict[str, Any] | None,
    trend_state_15m: dict[str, Any] | None,
    trend_state_5m: dict[str, Any] | None,
    ict_smc_state_1h: dict[str, Any] | None,
    ict_smc_state_15m: dict[str, Any] | None,
    ict_smc_state_5m: dict[str, Any] | None,
    ict_time_state: dict[str, Any] | None,
    price_action_state_1h: dict[str, Any] | None,
    price_action_state_5m: dict[str, Any] | None,
    current_close: float,
    is_universe_member: bool = True,
) -> FeatureRow:
    """Extract flat feature row from agent states at time `timestamp`."""
    # 1. HTF Trends
    t_2h = (trend_state_2h or {}).get("trend", "ranging")
    t_1h = (trend_state_1h or {}).get("trend", "ranging")

    # 2. Structure Events
    ev_15m = (trend_state_15m or {}).get("last_event")
    ev_15m_str = (
        f"{ev_15m.get('type', '')}_{ev_15m.get('direction', '')}" if isinstance(ev_15m, dict) else "none"
    )

    ev_5m = (trend_state_5m or {}).get("last_event")
    ev_5m_str = (
        f"{ev_5m.get('type', '')}_{ev_5m.get('direction', '')}" if isinstance(ev_5m, dict) else "none"
    )

    # 3. Premium / Discount & OTE
    smc_1h = ict_smc_state_1h or {}
    pd_range = smc_1h.get("pd_range") or {}
    pd_zone_1h = pd_range.get("zone", "none")
    pos_pct = pd_range.get("position_pct")
    in_ote_1h = bool(pd_range.get("in_ote", False))

    # 4. Nearest FVG
    fvgs_1h = smc_1h.get("fvgs") or []
    atr_1h = (trend_state_1h or {}).get("atr") or 1.0
    if atr_1h <= 0:
        atr_1h = 1.0

    nearest_fvg = None
    min_fvg_dist = float("inf")
    for fvg in fvgs_1h:
        if isinstance(fvg, dict) and fvg.get("state") in ("fresh", "partially_filled"):
            d_high = abs(current_close - fvg.get("high", current_close))
            d_low = abs(current_close - fvg.get("low", current_close))
            dist = min(d_high, d_low)
            if dist < min_fvg_dist:
                min_fvg_dist = dist
                nearest_fvg = fvg

    if nearest_fvg:
        fvg_size = abs(nearest_fvg.get("high", 0.0) - nearest_fvg.get("low", 0.0))
        nearest_fvg_size_atr = round(fvg_size / atr_1h, 4)
        nearest_fvg_displacement = bool(nearest_fvg.get("displacement_backed", False))
        nearest_fvg_dist_atr = round(min_fvg_dist / atr_1h, 4)
    else:
        nearest_fvg_size_atr = 0.0
        nearest_fvg_displacement = False
        nearest_fvg_dist_atr = 999.0

    # 5. Nearest Order Block
    obs_1h = smc_1h.get("order_blocks") or []
    nearest_ob = None
    min_ob_dist = float("inf")
    for ob in obs_1h:
        if isinstance(ob, dict) and ob.get("state") in ("fresh", "mitigated"):
            d_high = abs(current_close - ob.get("high", current_close))
            d_low = abs(current_close - ob.get("low", current_close))
            dist = min(d_high, d_low)
            if dist < min_ob_dist:
                min_ob_dist = dist
                nearest_ob = ob

    if nearest_ob:
        nearest_ob_state = str(nearest_ob.get("state", "none"))
        nearest_ob_dist_atr = round(min_ob_dist / atr_1h, 4)
    else:
        nearest_ob_state = "none"
        nearest_ob_dist_atr = 999.0

    # 6. Strong Pool Swept Recent
    last_sweep = smc_1h.get("last_sweep") or {}
    sweep_time = last_sweep.get("confirmed_at") or last_sweep.get("created_time")
    if sweep_time:
        bars_ago = max(0, int((timestamp - sweep_time) / 300_000))
        side = "BSL" if last_sweep.get("direction") == "up" else "SSL"
        strong_pool_swept_side = side if bars_ago <= 48 else "none"
        strong_pool_swept_bars_ago = bars_ago if bars_ago <= 48 else -1
    else:
        strong_pool_swept_side = "none"
        strong_pool_swept_bars_ago = -1

    # 7. Time Agent Features
    time_st = ict_time_state or {}
    killzone = str(time_st.get("current_killzone") or "none")
    silver_bullet_active = bool(time_st.get("current_silver_bullet") is not None)
    session = str(time_st.get("current_session") or "off_hours")

    key_levels_raw = time_st.get("key_levels")
    pdh_obj: dict[str, Any] = {}
    pdl_obj: dict[str, Any] = {}
    if isinstance(key_levels_raw, dict):
        pdh_obj = key_levels_raw.get("pdh") or {}
        pdl_obj = key_levels_raw.get("pdl") or {}
    elif isinstance(key_levels_raw, list):
        for kl in key_levels_raw:
            if isinstance(kl, dict):
                if kl.get("kind") == "pdh":
                    pdh_obj = kl
                elif kl.get("kind") == "pdl":
                    pdl_obj = kl

    dist_to_pdh_atr = (
        round((current_close - pdh_obj["price"]) / atr_1h, 4)
        if isinstance(pdh_obj, dict) and "price" in pdh_obj
        else None
    )
    dist_to_pdl_atr = (
        round((current_close - pdl_obj["price"]) / atr_1h, 4)
        if isinstance(pdl_obj, dict) and "price" in pdl_obj
        else None
    )

    pdh_swept_recent = (
        isinstance(pdh_obj, dict)
        and pdh_obj.get("state") in ("swept", "taken")
        and pdh_obj.get("swept_at") is not None
        and (timestamp - pdh_obj["swept_at"]) <= 12 * 300_000
    )
    pdl_swept_recent = (
        isinstance(pdl_obj, dict)
        and pdl_obj.get("state") in ("swept", "taken")
        and pdl_obj.get("swept_at") is not None
        and (timestamp - pdl_obj["swept_at"]) <= 12 * 300_000
    )

    # 8. Price Action Features
    pa_1h = price_action_state_1h or {}
    zones = pa_1h.get("zones") or []
    nearest_sr = None
    min_sr_dist = float("inf")
    for z in zones:
        if isinstance(z, dict) and z.get("state") == "active":
            d_high = abs(current_close - z.get("high", current_close))
            d_low = abs(current_close - z.get("low", current_close))
            dist = min(d_high, d_low)
            if dist < min_sr_dist:
                min_sr_dist = dist
                nearest_sr = z

    if nearest_sr:
        nearest_sr_strength = int(nearest_sr.get("touch_count", 0))
        nearest_sr_dist_atr = round(min_sr_dist / atr_1h, 4)
    else:
        nearest_sr_strength = 0
        nearest_sr_dist_atr = 999.0

    last_pat = pa_1h.get("last_pattern") or {}
    pattern_recent_type = str(last_pat.get("pattern", "none")) if isinstance(last_pat, dict) else "none"
    pattern_recent_at_zone = bool(last_pat.get("at_zone", False)) if isinstance(last_pat, dict) else False

    if isinstance(last_pat, dict) and "pattern" in last_pat:
        pat_name = last_pat["pattern"]
        if "bull" in pat_name or pat_name == "hammer":
            pattern_recent_with_trend = "aligned" if t_1h == "bullish" else "counter"
        elif "bear" in pat_name or pat_name == "shooting_star":
            pattern_recent_with_trend = "aligned" if t_1h == "bearish" else "counter"
        else:
            pattern_recent_with_trend = "none"
    else:
        pattern_recent_with_trend = "none"

    # 9. System & Volatility
    dt_utc = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
    weekend_flag = dt_utc.weekday() in (5, 6)
    atr_pct = round(atr_1h / current_close, 6) if current_close > 0 else 0.0

    return FeatureRow(
        timestamp=timestamp,
        symbol=symbol,
        htf_trend_2h=t_2h,
        htf_trend_1h=t_1h,
        last_event_15m=ev_15m_str,
        last_event_5m=ev_5m_str,
        pd_zone_1h=pd_zone_1h,
        position_pct_1h=pos_pct,
        in_ote_1h=in_ote_1h,
        nearest_fvg_size_atr=nearest_fvg_size_atr,
        nearest_fvg_displacement=nearest_fvg_displacement,
        nearest_fvg_dist_atr=nearest_fvg_dist_atr,
        nearest_ob_state=nearest_ob_state,
        nearest_ob_dist_atr=nearest_ob_dist_atr,
        strong_pool_swept_side=strong_pool_swept_side,
        strong_pool_swept_bars_ago=strong_pool_swept_bars_ago,
        killzone=killzone,
        silver_bullet_active=silver_bullet_active,
        session=session,
        dist_to_pdh_atr=dist_to_pdh_atr,
        dist_to_pdl_atr=dist_to_pdl_atr,
        pdh_swept_recent=pdh_swept_recent,
        pdl_swept_recent=pdl_swept_recent,
        nearest_sr_strength=nearest_sr_strength,
        nearest_sr_dist_atr=nearest_sr_dist_atr,
        pattern_recent_type=pattern_recent_type,
        pattern_recent_at_zone=pattern_recent_at_zone,
        pattern_recent_with_trend=pattern_recent_with_trend,
        weekend_flag=weekend_flag,
        atr_pct=atr_pct,
        universe_member=is_universe_member,
    )
