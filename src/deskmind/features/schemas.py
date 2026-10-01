"""Typed schemas for agent state() return values consumed by the feature builder.

Every agent's state() output is described here so that:

1. Missing or renamed keys raise ``KeyError`` immediately (no silent defaults).
2. The exact set of keys in each state dict is documented and enforced.
3. Schema drift between an agent's state() implementation and the builder is caught
   by the test suite before any feature data is written.

Usage in the builder::

    from deskmind.features.schemas import (
        validate_trend_state,
        validate_ict_smc_state,
        validate_ict_time_state,
        validate_price_action_state,
    )
    trend = validate_trend_state(raw_dict)

Validation is strict: every required top-level key must be present.
Optional keys (may be None) are allowed; extra keys are silently ignored.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# TrendEngine.state() schema
# ---------------------------------------------------------------------------

TREND_STATE_REQUIRED_KEYS: frozenset[str] = frozenset({
    "as_of",
    "trend",
    "reason",
    "last_swing_high",
    "last_swing_low",
    "last_event",
    "active_trendline",
    "atr",
})


def validate_trend_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a TrendEngine state dict.

    Required keys (values may be None):
    - ``as_of`` (int | None): epoch ms of last processed candle
    - ``trend`` (str): "bullish" | "bearish" | "ranging"
    - ``reason`` (str)
    - ``last_swing_high`` (dict | None): SwingPoint summary
    - ``last_swing_low`` (dict | None): SwingPoint summary
    - ``last_event`` (dict | None): StructureBreak summary with keys
        ``type`` ("BOS" | "CHoCH"), ``direction`` ("up" | "down"), ``confirmed_at``
    - ``active_trendline`` (dict | None)
    - ``atr`` (float | None)

    Raises KeyError if any required key is absent.
    """
    missing = TREND_STATE_REQUIRED_KEYS - raw.keys()
    if missing:
        raise KeyError(
            f"TrendEngine state() is missing required keys: {sorted(missing)}. "
            f"Got keys: {sorted(raw.keys())}. "
            "This means the engine's state() contract changed — update the schema."
        )
    return raw


# ---------------------------------------------------------------------------
# IctSmcEngine.state() schema
# ---------------------------------------------------------------------------

ICT_SMC_STATE_REQUIRED_KEYS: frozenset[str] = frozenset({
    "pd_range",
    "liquidity",
    "fvgs",
    "order_blocks",
    "breakers",
    "pd_arrays_in_zone",
    "displacement",
    "smt",
})

ICT_SMC_LIQUIDITY_REQUIRED_KEYS: frozenset[str] = frozenset({"bsl", "ssl", "last_sweep"})

# Keys required in each FVG summary dict (when not None)
ICT_SMC_FVG_REQUIRED_KEYS: frozenset[str] = frozenset({
    "id", "direction", "top", "bottom", "ce", "atr",
    "size_atr", "displacement_backed", "state", "created_time", "confirmed_at",
})

# Keys required in each OB summary dict
ICT_SMC_OB_REQUIRED_KEYS: frozenset[str] = frozenset({
    "id", "direction", "top", "bottom", "mt", "atr",
    "state", "created_time", "confirmed_at",
})


def validate_ict_smc_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and return an IctSmcEngine state dict.

    Top-level keys (all required; values may be None / empty list):
    - ``pd_range`` (dict | None): PDRange.as_dict() — keys: ``zone``, ``position_pct``, ``in_ote``
    - ``liquidity`` (dict): keys ``bsl`` (list), ``ssl`` (list), ``last_sweep`` (dict | None)
    - ``fvgs`` (list[dict]): FVG.summary() — each must have ``top``, ``bottom``,
        ``size_atr``, ``displacement_backed``, ``state``, ``direction``
    - ``order_blocks`` (list[dict]): OB.summary() — each must have ``top``, ``bottom``,
        ``state``, ``direction``
    - ``breakers`` (list[dict])
    - ``pd_arrays_in_zone`` (list[dict])
    - ``displacement`` (dict | None)
    - ``smt`` (dict | None)

    Raises KeyError if any required key is absent.
    """
    missing = ICT_SMC_STATE_REQUIRED_KEYS - raw.keys()
    if missing:
        raise KeyError(
            f"IctSmcEngine state() is missing required keys: {sorted(missing)}. "
            f"Got: {sorted(raw.keys())}"
        )

    liq = raw["liquidity"]
    if not isinstance(liq, dict):
        raise KeyError(
            f"IctSmcEngine state()['liquidity'] must be a dict, got {type(liq).__name__}"
        )
    missing_liq = ICT_SMC_LIQUIDITY_REQUIRED_KEYS - liq.keys()
    if missing_liq:
        raise KeyError(
            f"IctSmcEngine state()['liquidity'] is missing keys: {sorted(missing_liq)}"
        )

    for i, fvg in enumerate(raw.get("fvgs") or []):
        if isinstance(fvg, dict):
            missing_fvg = ICT_SMC_FVG_REQUIRED_KEYS - fvg.keys()
            if missing_fvg:
                raise KeyError(
                    f"FVG summary at index {i} is missing keys: {sorted(missing_fvg)}. "
                    "FVG.summary() must include size_atr and displacement_backed."
                )

    for i, ob in enumerate(raw.get("order_blocks") or []):
        if isinstance(ob, dict):
            missing_ob = ICT_SMC_OB_REQUIRED_KEYS - ob.keys()
            if missing_ob:
                raise KeyError(
                    f"OrderBlock summary at index {i} is missing keys: {sorted(missing_ob)}."
                )

    return raw


# ---------------------------------------------------------------------------
# IctTimeEngine.state() schema
# ---------------------------------------------------------------------------

ICT_TIME_STATE_REQUIRED_KEYS: frozenset[str] = frozenset({
    "sessions",
    "current_session",
    "killzone",
    "silver_bullet",
    "key_levels",
    "weekend_flag",
    "amd",
})

ICT_TIME_KILLZONE_REQUIRED_KEYS: frozenset[str] = frozenset({"current", "minutes_to_next"})
ICT_TIME_SILVER_BULLET_REQUIRED_KEYS: frozenset[str] = frozenset({"active", "fvg_in_windows"})

ICT_TIME_KEY_LEVEL_REQUIRED_KEYS: frozenset[str] = frozenset({
    "level_type", "price", "state",
})


def validate_ict_time_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and return an IctTimeEngine state dict.

    Required top-level keys:
    - ``sessions`` (dict)
    - ``current_session`` (str | None)
    - ``killzone`` (dict): must have ``current`` (str | None), ``minutes_to_next`` (int | None)
    - ``silver_bullet`` (dict): must have ``active`` (str | None), ``fvg_in_windows`` (list)
    - ``key_levels`` (list[dict]): each must have ``level_type`` (NOT ``kind``), ``price``, ``state``
    - ``weekend_flag`` (bool)
    - ``amd`` (dict)

    Raises KeyError if any required key or sub-key is absent.
    """
    missing = ICT_TIME_STATE_REQUIRED_KEYS - raw.keys()
    if missing:
        raise KeyError(
            f"IctTimeEngine state() is missing required keys: {sorted(missing)}. "
            f"Got: {sorted(raw.keys())}"
        )

    kz = raw["killzone"]
    if not isinstance(kz, dict):
        raise KeyError(
            f"IctTimeEngine state()['killzone'] must be a dict, got {type(kz).__name__}. "
            "The builder reads killzone['current'], not root-level 'current_killzone'."
        )
    missing_kz = ICT_TIME_KILLZONE_REQUIRED_KEYS - kz.keys()
    if missing_kz:
        raise KeyError(
            f"IctTimeEngine state()['killzone'] missing keys: {sorted(missing_kz)}"
        )

    sb = raw["silver_bullet"]
    if not isinstance(sb, dict):
        raise KeyError(
            f"IctTimeEngine state()['silver_bullet'] must be a dict, got {type(sb).__name__}"
        )
    missing_sb = ICT_TIME_SILVER_BULLET_REQUIRED_KEYS - sb.keys()
    if missing_sb:
        raise KeyError(
            f"IctTimeEngine state()['silver_bullet'] missing keys: {sorted(missing_sb)}"
        )

    for i, kl in enumerate(raw.get("key_levels") or []):
        if isinstance(kl, dict):
            if not ("level_type" in kl and "price" in kl and "state" in kl):
                missing_kl = ICT_TIME_KEY_LEVEL_REQUIRED_KEYS - kl.keys()
                raise KeyError(
                    f"key_level[{i}] missing keys: {sorted(missing_kl)}. "
                    "Use 'level_type' (not 'kind') to identify PDH/PDL."
                )

    return raw


# ---------------------------------------------------------------------------
# PriceActionEngine.state() schema
# ---------------------------------------------------------------------------

PRICE_ACTION_STATE_REQUIRED_KEYS: frozenset[str] = frozenset({
    "nearest_support",
    "nearest_resistance",
    "active_zones",
    "active_range",
    "recent_patterns",
    "recent_breakouts",
    "round_numbers",
    "atr",
})

PRICE_ACTION_ZONE_REQUIRED_KEYS: frozenset[str] = frozenset({
    "id", "side", "top", "bottom", "mid", "state", "strength", "touches",
})

PRICE_ACTION_PATTERN_REQUIRED_KEYS: frozenset[str] = frozenset({
    "pattern", "at_zone", "bullish",
})


def validate_price_action_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a PriceActionEngine state dict.

    Required top-level keys:
    - ``nearest_support`` (dict | None)
    - ``nearest_resistance`` (dict | None)
    - ``active_zones`` (list[dict]): NOT ``zones``; each must have ``top``, ``bottom``, ``mid``,
        ``touches`` (list) — NOT ``high``/``low``/``touch_count``
    - ``active_range`` (dict | None)
    - ``recent_patterns`` (list[dict]): NOT ``last_pattern``; each must have ``pattern`` (str),
        ``at_zone`` (bool | dict), ``bullish`` (bool)
    - ``recent_breakouts`` (list[dict])
    - ``round_numbers`` (list)
    - ``atr`` (float | None)

    Raises KeyError if any required key is absent.
    """
    missing = PRICE_ACTION_STATE_REQUIRED_KEYS - raw.keys()
    if missing:
        raise KeyError(
            f"PriceActionEngine state() is missing required keys: {sorted(missing)}. "
            f"Got: {sorted(raw.keys())}. "
            "Use 'active_zones' (not 'zones') and 'recent_patterns' (not 'last_pattern')."
        )

    for i, z in enumerate(raw.get("active_zones") or []):
        if isinstance(z, dict):
            missing_z = PRICE_ACTION_ZONE_REQUIRED_KEYS - z.keys()
            if missing_z:
                raise KeyError(
                    f"active_zones[{i}] missing keys: {sorted(missing_z)}. "
                    "Zone items use 'top'/'bottom'/'mid'/'touches' (not 'high'/'low'/'touch_count')."
                )

    for i, pat in enumerate(raw.get("recent_patterns") or []):
        if isinstance(pat, dict):
            missing_pat = PRICE_ACTION_PATTERN_REQUIRED_KEYS - pat.keys()
            if missing_pat:
                raise KeyError(
                    f"recent_patterns[{i}] missing keys: {sorted(missing_pat)}"
                )

    return raw
