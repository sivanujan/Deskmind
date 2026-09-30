"""Tests for pre-registered hypotheses H1-H5 strategy plugins."""

from __future__ import annotations

import pytest

from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin
from deskmind.research.hypotheses.registry import REGISTRY


def test_registry_contains_all_five_hypotheses() -> None:
    plugins = REGISTRY.list_hypotheses()
    assert len(plugins) == 5
    ids = {p.hypothesis_id for p in plugins}
    assert ids == {"H1_v1.0.0", "H2_v1.0.0", "H3_v1.0.0", "H4_v1.0.0", "H5_v1.0.0"}


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
