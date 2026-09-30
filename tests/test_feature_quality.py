"""Tests for feature data-quality check and gate."""

from __future__ import annotations

import pytest

from deskmind.features.quality import (
    FeatureQualityError,
    check_feature_quality,
    feature_quality_report,
)


def _make_dummy_rows(n: int = 15) -> list[dict]:
    return [
        {
            "timestamp": 1704067200000 + i * 300_000,
            "symbol": "BTCUSDT",
            "htf_trend_2h": "bullish" if i % 2 == 0 else "bearish",
            "htf_trend_1h": "bullish" if i % 3 == 0 else "bearish",
            "last_event_15m": "BOS_up" if i % 2 == 0 else "none",
            "last_event_5m": "CHoCH_up" if i % 4 == 0 else "none",
            "pd_zone_1h": "discount" if i % 2 == 0 else "premium",
            "position_pct_1h": 0.45 if i % 2 == 0 else 0.65,
            "in_ote_1h": i % 3 == 0,
            "nearest_fvg_size_atr": 0.5 + (i * 0.1),
            "nearest_fvg_displacement": i % 2 == 0,
            "nearest_fvg_dist_atr": 0.2 + (i * 0.05),
            "nearest_ob_state": "fresh" if i % 2 == 0 else "mitigated",
            "nearest_ob_dist_atr": 0.3 + (i * 0.05),
            "strong_pool_swept_side": "SSL" if i % 3 == 0 else "none",
            "strong_pool_swept_bars_ago": i if i % 3 == 0 else -1,
            "killzone": "london_open" if i % 2 == 0 else "ny_am",
            "silver_bullet_active": i % 4 == 0,
            "session": "london" if i % 2 == 0 else "new_york",
            "dist_to_pdh_atr": 1.2 + (i * 0.1),
            "dist_to_pdl_atr": 1.5 + (i * 0.1),
            "pdh_swept_recent": i % 5 == 0,
            "pdl_swept_recent": i % 7 == 0,
            "nearest_sr_strength": i % 3,
            "nearest_sr_dist_atr": 0.4 + (i * 0.05),
            "pattern_recent_type": "hammer" if i % 2 == 0 else "engulfing",
            "pattern_recent_at_zone": i % 3 == 0,
            "pattern_recent_with_trend": "aligned" if i % 2 == 0 else "none",
            "weekend_flag": False,
            "atr_pct": 0.005 + (i * 0.0001),
            "universe_member": True,
        }
        for i in range(n)
    ]


def test_feature_quality_check_passes_on_valid_rows() -> None:
    rows = _make_dummy_rows(20)
    check_feature_quality(rows)


def test_feature_quality_check_fails_on_100_percent_null() -> None:
    rows = _make_dummy_rows(20)
    # Make nearest_fvg_size_atr 100% null
    for r in rows:
        r["nearest_fvg_size_atr"] = None

    with pytest.raises(FeatureQualityError, match="100% NULL features"):
        check_feature_quality(rows)


def test_feature_quality_check_fails_on_single_constant_value() -> None:
    rows = _make_dummy_rows(20)
    # Make killzone always "none" (the classic builder bug)
    for r in rows:
        r["killzone"] = "none"

    with pytest.raises(FeatureQualityError, match="Constant-value features"):
        check_feature_quality(rows)


def test_feature_quality_check_allows_nullable_features_to_be_null() -> None:
    rows = _make_dummy_rows(20)
    # dist_to_pdh_atr is allowed to be null early in warmup
    for r in rows:
        r["dist_to_pdh_atr"] = None
        r["dist_to_pdl_atr"] = None
    check_feature_quality(rows)


def test_feature_quality_report_generates_table() -> None:
    rows = _make_dummy_rows(15)
    report = feature_quality_report(rows)
    assert len(report) > 0
    f_names = {item["feature"] for item in report}
    assert "killzone" in f_names
    assert "htf_trend_1h" in f_names
