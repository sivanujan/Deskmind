"""Tests for agent state schemas and builder validation."""

from __future__ import annotations

import pytest

from deskmind.features.schemas import (
    validate_ict_smc_state,
    validate_ict_time_state,
    validate_price_action_state,
    validate_trend_state,
)


def test_validate_trend_state_success() -> None:
    valid_state = {
        "as_of": 1704067200000,
        "trend": "bullish",
        "reason": "BOS up",
        "last_swing_high": {"price": 105.0},
        "last_swing_low": {"price": 95.0},
        "last_event": {"type": "BOS", "direction": "up", "confirmed_at": 1704067200000},
        "active_trendline": None,
        "atr": 2.5,
    }
    assert validate_trend_state(valid_state) == valid_state


def test_validate_trend_state_missing_key_raises() -> None:
    partial_state = {"trend": "bullish"}
    with pytest.raises(KeyError, match="TrendEngine state\\(\\) is missing required keys"):
        validate_trend_state(partial_state)


def test_validate_ict_smc_state_success() -> None:
    valid_state = {
        "pd_range": {"zone": "discount", "equilibrium": 100.0},
        "liquidity": {
            "bsl": [],
            "ssl": [],
            "last_sweep": None,
        },
        "fvgs": [
            {
                "id": "fvg_1",
                "direction": "bullish",
                "top": 102.0,
                "bottom": 100.0,
                "ce": 101.0,
                "atr": 2.0,
                "size_atr": 1.0,
                "displacement_backed": True,
                "state": "active",
                "created_time": 1000,
                "confirmed_at": 2000,
            }
        ],
        "order_blocks": [
            {
                "id": "ob_1",
                "direction": "bullish",
                "top": 99.0,
                "bottom": 97.0,
                "mt": 98.0,
                "atr": 2.0,
                "state": "fresh",
                "created_time": 1000,
                "confirmed_at": 2000,
            }
        ],
        "breakers": [],
        "pd_arrays_in_zone": [],
        "displacement": None,
        "smt": None,
    }
    assert validate_ict_smc_state(valid_state) == valid_state


def test_validate_ict_smc_state_missing_liquidity_raises() -> None:
    bad_state = {
        "pd_range": None,
        "liquidity": {"bsl": []},  # missing ssl, last_sweep
        "fvgs": [],
        "order_blocks": [],
        "breakers": [],
        "pd_arrays_in_zone": [],
        "displacement": None,
        "smt": None,
    }
    with pytest.raises(KeyError, match=r"liquidity.*is missing keys"):
        validate_ict_smc_state(bad_state)


def test_validate_ict_smc_fvg_missing_size_atr_raises() -> None:
    bad_fvg_state = {
        "pd_range": None,
        "liquidity": {"bsl": [], "ssl": [], "last_sweep": None},
        "fvgs": [
            {
                "id": "fvg_1",
                "direction": "bullish",
                "top": 102.0,
                "bottom": 100.0,
                "ce": 101.0,
                "atr": 2.0,
                # missing size_atr and displacement_backed
                "state": "active",
                "created_time": 1000,
                "confirmed_at": 2000,
            }
        ],
        "order_blocks": [],
        "breakers": [],
        "pd_arrays_in_zone": [],
        "displacement": None,
        "smt": None,
    }
    with pytest.raises(KeyError, match=r"FVG summary at index 0 is missing keys"):
        validate_ict_smc_state(bad_fvg_state)


def test_validate_ict_time_state_success() -> None:
    valid_state = {
        "sessions": {},
        "current_session": "london",
        "killzone": {"current": "london_open", "minutes_to_next": None},
        "silver_bullet": {"active": None, "fvg_in_windows": []},
        "key_levels": [
            {"level_type": "pdh", "price": 65000.0, "state": "active"}
        ],
        "weekend_flag": False,
        "amd": None,
    }
    assert validate_ict_time_state(valid_state) == valid_state


def test_validate_ict_time_state_missing_killzone_raises() -> None:
    bad_time_state = {
        "sessions": {},
        "current_session": "london",
        "killzone": "london_open",  # must be dict with 'current'
        "silver_bullet": {"active": None, "fvg_in_windows": []},
        "key_levels": [],
        "weekend_flag": False,
        "amd": None,
    }
    with pytest.raises(KeyError, match=r"must be a dict"):
        validate_ict_time_state(bad_time_state)


def test_validate_price_action_state_success() -> None:
    valid_state = {
        "nearest_support": None,
        "nearest_resistance": None,
        "active_zones": [
            {
                "id": "z_1",
                "side": "support",
                "top": 100.0,
                "bottom": 98.0,
                "mid": 99.0,
                "state": "active",
                "strength": 2,
                "touches": [1000, 2000],
            }
        ],
        "active_range": None,
        "recent_patterns": [
            {"pattern": "hammer", "at_zone": True, "bullish": True}
        ],
        "recent_breakouts": [],
        "round_numbers": [100.0],
        "atr": 1.5,
    }
    assert validate_price_action_state(valid_state) == valid_state


def test_validate_price_action_state_missing_keys_raises() -> None:
    bad_pa_state = {"zones": []}
    with pytest.raises(KeyError, match="PriceActionEngine state\\(\\) is missing required keys"):
        validate_price_action_state(bad_pa_state)
