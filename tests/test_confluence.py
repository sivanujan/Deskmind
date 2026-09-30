"""Tests for MTF Alignment & Equal-Weight Confluence Engine."""

from __future__ import annotations

import pytest

from deskmind.confluence.engine import evaluate_confluence
from deskmind.confluence.models import ConfluenceResult


def test_confluence_aligned_bullish() -> None:
    row = {
        "htf_trend_2h": "bullish",
        "htf_trend_1h": "bullish",
        "last_event_15m": "BOS_up",
        "last_event_5m": "CHoCH_up",
        "pd_zone_1h": "discount",
        "in_ote_1h": True,
        "nearest_ob_dist_atr": 0.5,
        "nearest_ob_state": "fresh",
        "nearest_fvg_dist_atr": 0.3,
        "nearest_fvg_size_atr": 0.6,
        "strong_pool_swept_side": "SSL",
        "strong_pool_swept_bars_ago": 5,
        "killzone": "london_open",
        "silver_bullet_active": False,
        "pdh_swept_recent": False,
        "pdl_swept_recent": True,
        "nearest_sr_strength": 3,
        "pattern_recent_at_zone": True,
    }

    res = evaluate_confluence(row)
    assert res.alignment == "aligned"
    assert res.bias == "bullish"
    assert res.score == 10.0
    assert res.components["htf_trend_aligned"] == 1.0
    assert res.components["pd_zone_aligned"] == 1.0


def test_confluence_conflict() -> None:
    row = {
        "htf_trend_2h": "bullish",
        "htf_trend_1h": "bullish",
        "last_event_15m": "BOS_down",
        "last_event_5m": "CHoCH_down",  # 5m trigger opposes 1h trend
        "pd_zone_1h": "premium",
    }

    res = evaluate_confluence(row)
    assert res.alignment == "conflict"
    assert res.bias == "bullish"
    assert res.score < 5.0
