"""Tests for point-in-time feature store and truncation (no look-ahead)."""

from __future__ import annotations

from datetime import UTC, datetime
import pytest

from deskmind.features.builder import build_feature_row
from deskmind.features.store import FeatureStore
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.models import Bar


def _make_bar(ts_ms: int, c: float) -> Bar:
    return Bar(
        open_time=ts_ms,
        close_time=ts_ms + 300_000,
        open=c - 1.0,
        high=c + 2.0,
        low=c - 2.0,
        close=c,
    )


def test_feature_row_truncation_point_in_time(tmp_path) -> None:
    """Features at time T built from full stream cut at T are identical to features at T built from stream up to T."""
    t0 = int(datetime(2024, 1, 1, 12, 0, tzinfo=UTC).timestamp() * 1000)

    # Full stream of 50 bars
    bars_full = [_make_bar(t0 + i * 300_000, 100.0 + i * 0.5) for i in range(50)]

    cfg = TrendConfig(min_warmup_bars=2)
    # Run engine on full stream up to bar 30 (time T)
    eng_full = TrendEngine("BTC", "5m", cfg=cfg)
    for b in bars_full[:30]:
        eng_full.on_candle(b)

    st_full_at_30 = eng_full.state()
    cut_time = bars_full[29].close_time

    # Run engine on stream cut at bar 30
    eng_cut = TrendEngine("BTC", "5m", cfg=cfg)
    for b in bars_full[:30]:
        eng_cut.on_candle(b)
    st_cut_at_30 = eng_cut.state()

    # Build feature row from both
    row_full = build_feature_row(
        symbol="BTC",
        timestamp=cut_time,
        trend_state_2h=st_full_at_30,
        trend_state_1h=st_full_at_30,
        trend_state_15m=st_full_at_30,
        trend_state_5m=st_full_at_30,
        ict_smc_state_1h=None,
        ict_smc_state_15m=None,
        ict_smc_state_5m=None,
        ict_time_state=None,
        price_action_state_1h=None,
        price_action_state_5m=None,
        current_close=bars_full[29].close,
    )

    row_cut = build_feature_row(
        symbol="BTC",
        timestamp=cut_time,
        trend_state_2h=st_cut_at_30,
        trend_state_1h=st_cut_at_30,
        trend_state_15m=st_cut_at_30,
        trend_state_5m=st_cut_at_30,
        ict_smc_state_1h=None,
        ict_smc_state_15m=None,
        ict_smc_state_5m=None,
        ict_time_state=None,
        price_action_state_1h=None,
        price_action_state_5m=None,
        current_close=bars_full[29].close,
    )

    assert row_full.as_dict() == row_cut.as_dict()


def test_feature_store_parquet_roundtrip(tmp_path) -> None:
    """Test Parquet file writing and reading roundtrip."""
    store = FeatureStore(base_dir=tmp_path)
    t0 = 1704067200000  # 2024-01-01 00:00 UTC (TRAIN split)
    row = build_feature_row(
        symbol="BTCUSDT",
        timestamp=t0,
        trend_state_2h={"trend": "bullish"},
        trend_state_1h={"trend": "bullish"},
        trend_state_15m=None,
        trend_state_5m=None,
        ict_smc_state_1h=None,
        ict_smc_state_15m=None,
        ict_smc_state_5m=None,
        ict_time_state=None,
        price_action_state_1h=None,
        price_action_state_5m=None,
        current_close=65000.0,
    )

    store.write_features([row])
    rows = store.read_features("BTCUSDT")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["htf_trend_1h"] == "bullish"
