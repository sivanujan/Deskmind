"""Feature row builder from point-in-time agent states.

Rules:
- All agent state dicts are validated through ``schemas.py`` before any key is read.
  A missing or renamed key raises ``KeyError`` immediately — no silent defaults.
- Feature values derived from absent optional objects (no FVG found, no PDH level yet,
  etc.) use sentinel values: 0.0 / 999.0 / "none" / False as documented per feature.
- Silent ``or {}`` / ``or []`` is allowed ONLY for the top-level optional agent state
  argument (the caller may pass None when an agent has no data yet).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.features.schemas import (
    validate_ict_smc_state,
    validate_ict_time_state,
    validate_price_action_state,
    validate_trend_state,
)


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
    """Extract flat feature row from agent states at time ``timestamp``.

    All agent state dicts are validated by ``schemas.py`` before use.
    A ``None`` argument means the agent has no data yet (e.g. cold start);
    an empty dict ``{}`` will raise ``KeyError`` because required keys are absent.
    """
    # ------------------------------------------------------------------ 1. HTF Trends
    # Sentinel "ranging" when no trend agent data yet.
    if trend_state_2h is not None:
        _t2 = validate_trend_state(trend_state_2h)
        t_2h: str = _t2["trend"] or "ranging"
    else:
        t_2h = "ranging"

    if trend_state_1h is not None:
        _t1 = validate_trend_state(trend_state_1h)
        t_1h: str = _t1["trend"] or "ranging"
        atr_1h: float = float(_t1["atr"] or 0.0) or 1.0
    else:
        t_1h = "ranging"
        atr_1h = 1.0
    if atr_1h <= 0:
        atr_1h = 1.0

    # ------------------------------------------------------------------ 2. Structure Events
    if trend_state_15m is not None:
        _t15 = validate_trend_state(trend_state_15m)
        ev_15m = _t15["last_event"]
    else:
        ev_15m = None
    ev_15m_str = (
        f"{ev_15m.get('type', '')}_{ev_15m.get('direction', '')}"
        if isinstance(ev_15m, dict)
        else "none"
    )

    if trend_state_5m is not None:
        _t5 = validate_trend_state(trend_state_5m)
        ev_5m = _t5["last_event"]
    else:
        ev_5m = None
    ev_5m_str = (
        f"{ev_5m.get('type', '')}_{ev_5m.get('direction', '')}"
        if isinstance(ev_5m, dict)
        else "none"
    )

    # ------------------------------------------------------------------ 3. Premium/Discount & OTE
    if ict_smc_state_1h is not None:
        smc_1h = validate_ict_smc_state(ict_smc_state_1h)
    else:
        smc_1h = None

    pd_range = (smc_1h or {}).get("pd_range") or {}
    pd_zone_1h: str = pd_range.get("zone", "none") or "none"
    pos_pct: float | None = pd_range.get("position_pct")
    in_ote_1h: bool = bool(pd_range.get("in_ote", False))

    # ------------------------------------------------------------------ 4. Nearest FVG
    # FVG summary() keys: id, direction, top, bottom, ce, atr, size_atr,
    #                     displacement_backed, state, created_time, confirmed_at
    # Distance uses top/bottom (NOT high/low — FVGs have no high/low key).
    fvgs_1h: list[dict[str, Any]] = (smc_1h or {}).get("fvgs") or []

    nearest_fvg: dict[str, Any] | None = None
    min_fvg_dist = float("inf")
    for fvg in fvgs_1h:
        if isinstance(fvg, dict) and fvg.get("state") in ("fresh", "partially_filled"):
            d_top = abs(current_close - fvg["top"])
            d_bot = abs(current_close - fvg["bottom"])
            dist = min(d_top, d_bot)
            if dist < min_fvg_dist:
                min_fvg_dist = dist
                nearest_fvg = fvg

    if nearest_fvg:
        # size_atr is pre-computed by the engine at FVG confirmation time.
        fvg_size_atr = nearest_fvg.get("size_atr")
        nearest_fvg_size_atr: float = float(fvg_size_atr) if fvg_size_atr is not None else round(
            abs(nearest_fvg["top"] - nearest_fvg["bottom"]) / atr_1h, 4
        )
        nearest_fvg_displacement: bool = bool(nearest_fvg.get("displacement_backed", False))
        nearest_fvg_dist_atr: float = round(min_fvg_dist / atr_1h, 4)
    else:
        nearest_fvg_size_atr = 0.0
        nearest_fvg_displacement = False
        nearest_fvg_dist_atr = 999.0

    # ------------------------------------------------------------------ 5. Nearest Order Block
    # OB summary() keys: id, direction, top, bottom, mt, atr, state, created_time, confirmed_at
    # Distance uses top/bottom (NOT high/low).
    obs_1h: list[dict[str, Any]] = (smc_1h or {}).get("order_blocks") or []

    nearest_ob: dict[str, Any] | None = None
    min_ob_dist = float("inf")
    for ob in obs_1h:
        if isinstance(ob, dict) and ob.get("state") in ("fresh", "tested"):
            d_top = abs(current_close - ob["top"])
            d_bot = abs(current_close - ob["bottom"])
            dist = min(d_top, d_bot)
            if dist < min_ob_dist:
                min_ob_dist = dist
                nearest_ob = ob

    if nearest_ob:
        nearest_ob_state: str = str(nearest_ob["state"])
        nearest_ob_dist_atr: float = round(min_ob_dist / atr_1h, 4)
    else:
        nearest_ob_state = "none"
        nearest_ob_dist_atr = 999.0

    # ------------------------------------------------------------------ 6. Strong Pool Swept Recent
    # last_sweep is inside state()['liquidity'], NOT at the state() root.
    liq_1h: dict[str, Any] = (smc_1h or {}).get("liquidity") or {}
    last_sweep: dict[str, Any] = liq_1h.get("last_sweep") or {}
    sweep_time: int | None = last_sweep.get("confirmed_at") or last_sweep.get("created_time")
    if sweep_time:
        bars_ago = max(0, int((timestamp - sweep_time) / 300_000))
        side = "BSL" if last_sweep.get("direction") == "up" else "SSL"
        strong_pool_swept_side: str = side if bars_ago <= 48 else "none"
        strong_pool_swept_bars_ago: int = bars_ago if bars_ago <= 48 else -1
    else:
        strong_pool_swept_side = "none"
        strong_pool_swept_bars_ago = -1

    # ------------------------------------------------------------------ 7. Time Agent Features
    # IctTimeEngine.state() structure (validated by schema):
    #   killzone      -> {"current": str|None, "minutes_to_next": int|None}
    #   silver_bullet -> {"active": str|None, "fvg_in_windows": list}
    #   key_levels    -> list of dicts with "level_type" (NOT "kind"), "price", "state"
    if ict_time_state is not None:
        time_st = validate_ict_time_state(ict_time_state)
    else:
        time_st = None

    kz_dict: dict[str, Any] = (time_st or {}).get("killzone") or {}
    killzone: str = str(kz_dict.get("current") or "none")

    sb_dict: dict[str, Any] = (time_st or {}).get("silver_bullet") or {}
    silver_bullet_active: bool = sb_dict.get("active") is not None

    session: str = str((time_st or {}).get("current_session") or "off_hours")

    # PDH/PDL: key_levels list, each dict has "level_type" (not "kind") and "price".
    key_levels_raw: list[dict[str, Any]] = (time_st or {}).get("key_levels") or []
    pdh_obj: dict[str, Any] = {}
    pdl_obj: dict[str, Any] = {}
    for kl in reversed(key_levels_raw):
        if isinstance(kl, dict):
            lt = kl.get("level_type")  # "pdh" | "pdl" | "pwh" | …
            if lt == "pdh" and not pdh_obj:
                pdh_obj = kl
            elif lt == "pdl" and not pdl_obj:
                pdl_obj = kl

    dist_to_pdh_atr: float | None = (
        round((current_close - pdh_obj["price"]) / atr_1h, 4)
        if pdh_obj and "price" in pdh_obj
        else None
    )
    dist_to_pdl_atr: float | None = (
        round((current_close - pdl_obj["price"]) / atr_1h, 4)
        if pdl_obj and "price" in pdl_obj
        else None
    )

    pdh_action_at = (pdh_obj.get("swept_at") or pdh_obj.get("taken_at")) if pdh_obj else None
    pdh_swept_recent: bool = (
        bool(pdh_obj)
        and pdh_obj.get("state") in ("swept", "taken")
        and pdh_action_at is not None
        and (timestamp - pdh_action_at) <= 12 * 300_000
    )
    pdl_action_at = (pdl_obj.get("swept_at") or pdl_obj.get("taken_at")) if pdl_obj else None
    pdl_swept_recent: bool = (
        bool(pdl_obj)
        and pdl_obj.get("state") in ("swept", "taken")
        and pdl_action_at is not None
        and (timestamp - pdl_action_at) <= 12 * 300_000
    )

    # ------------------------------------------------------------------ 8. Price Action Features
    # PriceActionEngine.state() structure (validated by schema):
    #   active_zones    -> list of dicts (NOT "zones"); each has "top"/"bottom"/"mid"/"touches"
    #   recent_patterns -> list of dicts (NOT "last_pattern"); most recent is [-1]
    #   Zone distance uses mid-price (not high/low).
    #   Touch strength is len(touches) (not touch_count scalar).
    if price_action_state_1h is not None:
        pa_1h = validate_price_action_state(price_action_state_1h)
    else:
        pa_1h = None

    active_zones: list[dict[str, Any]] = (pa_1h or {}).get("active_zones") or []
    nearest_sr: dict[str, Any] | None = None
    min_sr_dist = float("inf")
    for z in active_zones:
        if isinstance(z, dict) and z.get("state") not in ("retired", None):
            mid: float = z.get("mid", current_close)
            dist = abs(current_close - mid)
            if dist < min_sr_dist:
                min_sr_dist = dist
                nearest_sr = z

    if nearest_sr:
        # touches is a list (timestamps); its length is the touch count.
        nearest_sr_strength: int = int(len(nearest_sr.get("touches") or []))
        nearest_sr_dist_atr: float = round(min_sr_dist / atr_1h, 4)
    else:
        nearest_sr_strength = 0
        nearest_sr_dist_atr = 999.0

    # Most recent pattern is last element in recent_patterns list (NOT last_pattern key).
    recent_pats: list[dict[str, Any]] = (pa_1h or {}).get("recent_patterns") or []
    last_pat: dict[str, Any] = recent_pats[-1] if recent_pats else {}
    pattern_recent_type: str = (
        str(last_pat.get("pattern", "none")) if isinstance(last_pat, dict) else "none"
    )
    pattern_recent_at_zone: bool = (
        bool(last_pat.get("at_zone")) if isinstance(last_pat, dict) else False
    )

    if isinstance(last_pat, dict) and "pattern" in last_pat:
        pat_name = last_pat["pattern"]
        if "bull" in pat_name or pat_name == "hammer":
            pattern_recent_with_trend: str = "aligned" if t_1h == "bullish" else "counter"
        elif "bear" in pat_name or pat_name == "shooting_star":
            pattern_recent_with_trend = "aligned" if t_1h == "bearish" else "counter"
        else:
            pattern_recent_with_trend = "none"
    else:
        pattern_recent_with_trend = "none"

    # ------------------------------------------------------------------ 9. System & Volatility
    dt_utc = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
    weekend_flag: bool = dt_utc.weekday() in (5, 6)
    atr_pct: float = round(atr_1h / current_close, 6) if current_close > 0 else 0.0

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
