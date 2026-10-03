"""Tests for pre-registered hypotheses H1-H5 strategy plugins."""

from __future__ import annotations

import pytest

from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin
from deskmind.research.hypotheses.registry import REGISTRY


def test_registry_contains_all_hypotheses() -> None:
    plugins = REGISTRY.list_hypotheses()
    assert len(plugins) == 6
    ids = {p.hypothesis_id for p in plugins}
    assert ids == {"H1_v1.0.0", "H2_v1.0.0", "H3_v1.0.0", "H4_v1.1.0", "H5_v1.0.0", "H6_v1.0.0"}


def test_plugin_names_equal_preregistered_names() -> None:
    """Verify hypothesis plugin names equal the pre-registered names in docs/research/hypotheses.md."""
    expected_names = {
        "H1_v1.0.0": "Discount OB / FVG Continuation",
        "H2_v1.0.0": "Liquidity Sweep Reversal",
        "H3_v1.0.0": "Silver Bullet Window FVG",
        "H4_v1.1.0": "Support/Resistance Breakout-Retest",
        "H5_v1.0.0": "PDH/PDL Sweep Reversal",
        "H6_v1.0.0": "4h Trend Breakout",
    }
    plugins = REGISTRY.list_hypotheses()
    for p in plugins:
        assert p.hypothesis_id in expected_names
        assert p.name == expected_names[p.hypothesis_id], (
            f"Plugin {p.hypothesis_id} name '{p.name}' does not match "
            f"pre-registered name '{expected_names[p.hypothesis_id]}'"
        )


def test_h1_discount_ob_evaluates_bullish_signal() -> None:
    p = H1DiscountOBPlugin()
    row = {
        "symbol": "BTCUSDT",
        "timestamp": 1750000000000,
        "htf_trend_1h": "bullish",
        "pd_zone_1h": "discount",
        "nearest_fvg_size_atr": 0.6,
        "nearest_fvg_dist_atr": 0.2,
        "nearest_ob_state": "fresh",
        "nearest_ob_dist_atr": 0.4,
        "last_event_5m": "CHoCH_up",
        "atr_pct": 0.01,
    }
    sig = p.evaluate(row, current_close=65000.0)
    assert sig is not None
    assert sig.direction == "bullish"
    assert sig.hypothesis_id == "H1_v1.0.0"
    assert sig.r_multiple == 2.0


def test_h2_sweep_reversal_evaluates_bullish_signal() -> None:
    p = H2SweepReversalPlugin()
    row = {
        "symbol": "BTCUSDT",
        "timestamp": 1750000000000,
        "killzone": "london_open",
        "strong_pool_swept_side": "SSL",
        "strong_pool_swept_bars_ago": 4,
        "last_event_5m": "CHoCH_up",
        "atr_pct": 0.01,
    }
    sig = p.evaluate(row, current_close=65000.0)
    assert sig is not None
    assert sig.direction == "bullish"
    assert sig.hypothesis_id == "H2_v1.0.0"


def test_h3_silver_bullet_evaluates_signal() -> None:
    p = H3SilverBulletPlugin()
    row = {
        "symbol": "BTCUSDT",
        "timestamp": 1750000000000,
        "silver_bullet_active": True,
        "htf_trend_1h": "bullish",
        "nearest_fvg_displacement": True,
        "nearest_fvg_dist_atr": 0.3,
        "nearest_fvg_size_atr": 0.4,
        "atr_pct": 0.01,
    }
    sig = p.evaluate(row, current_close=65000.0)
    assert sig is not None
    assert sig.direction == "bullish"


def test_h4_breakout_retest_v110_evaluates_signal() -> None:
    p = H4BreakoutRetestPlugin()
    row = {
        "symbol": "BTCUSDT",
        "timestamp": 1750000000000,
        "htf_trend_1h": "bullish",
        "nearest_sr_touches": 3,
        "nearest_sr_dist_atr": 0.2,
        "pattern_recent_at_zone": True,
        "pattern_recent_with_trend": "aligned",
        "pattern_recent_bars_ago": 2,
        "atr_pct": 0.01,
    }
    sig = p.evaluate(row, current_close=65000.0)
    assert sig is not None
    assert sig.direction == "bullish"
    assert sig.hypothesis_id == "H4_v1.1.0"
    assert sig.r_multiple == 2.0


def test_h5_pd_sweep_evaluates_signal() -> None:
    p = H5PDSweepPlugin()
    row = {
        "symbol": "BTCUSDT",
        "timestamp": 1750000000000,
        "killzone": "ny_am",
        "pdh_swept_recent": True,
        "last_event_5m": "CHoCH_down",
        "atr_pct": 0.01,
    }
    sig = p.evaluate(row, current_close=65000.0)
    assert sig is not None
    assert sig.direction == "bearish"
