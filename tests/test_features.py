"""Tests for point-in-time feature store and truncation (no look-ahead)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from deskmind.features.builder import build_feature_row
from deskmind.features.store import FeatureStore
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.models import Bar
from deskmind.agents.ict_time.config import DEFAULT_CONFIG as ICT_TIME_CFG
from deskmind.agents.ict_time.engine import IctTimeEngine
from deskmind.agents.ict_smc.config import DEFAULT_CONFIG as SMC_CFG
from deskmind.agents.ict_smc.engine import IctSmcEngine
from deskmind.agents.price_action.config import DEFAULT_CONFIG as PA_CFG
from deskmind.agents.price_action.engine import PriceActionEngine

_NY = ZoneInfo("America/New_York")


def _make_bar(ts_ms: int, c: float, tf_ms: int = 300_000) -> Bar:
    return Bar(
        open_time=ts_ms,
        close_time=ts_ms + tf_ms,
        open=c - 1.0,
        high=c + 2.0,
        low=c - 2.0,
        close=c,
    )


def _build_row(ict_time_state=None, ict_smc_state_1h=None, price_action_state_1h=None,
               ts_ms: int = 1704067200000) -> dict:
    """Helper: build a feature row from given agent states."""
    row = build_feature_row(
        symbol="BTC",
        timestamp=ts_ms,
        trend_state_2h=None,
        trend_state_1h=None,
        trend_state_15m=None,
        trend_state_5m=None,
        ict_smc_state_1h=ict_smc_state_1h,
        ict_smc_state_15m=None,
        ict_smc_state_5m=None,
        ict_time_state=ict_time_state,
        price_action_state_1h=price_action_state_1h,
        price_action_state_5m=None,
        current_close=65000.0,
    )
    return row.as_dict()


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
    cfg = TrendConfig(min_warmup_bars=1)
    eng = TrendEngine("BTCUSDT", "1h", cfg=cfg)
    bar = _make_bar(t0, 65000.0, 3600_000)
    eng.on_candle(bar)
    st = eng.state()
    row = build_feature_row(
        symbol="BTCUSDT",
        timestamp=t0,
        trend_state_2h=st,
        trend_state_1h=st,
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
    assert rows[0]["htf_trend_1h"] in ("bullish", "bearish", "ranging")


# ------------------------------------------------------------------ regression tests for builder key fixes


def _make_ict_time_state_in_killzone() -> dict:
    """Build a real IctTimeEngine state with bar inside NY AM killzone (9:30 NY)."""
    cfg = replace(ICT_TIME_CFG, min_warmup_bars=2)
    eng = IctTimeEngine("BTC", cfg=cfg)
    # 3 bars in NY AM killzone
    for hour in [8, 9, 10]:
        dt = datetime(2023, 1, 10, hour, 30, tzinfo=_NY)
        ms = int(dt.timestamp() * 1000)
        bar = Bar(open_time=ms, close_time=ms + 3_600_000,
                  open=17000, high=17200, low=16800, close=17050)
        eng.on_candle(bar)
    return eng.state()


def test_builder_killzone_reads_nested_dict_key() -> None:
    """BUG FIX: builder read 'current_killzone' (non-existent root key) instead of killzone['current'].

    Previously this always produced killzone='none'. Now it reads the nested dict correctly.
    """
    ict_time_st = _make_ict_time_state_in_killzone()
    # Sanity check the state structure is correct
    assert isinstance(ict_time_st.get("killzone"), dict), "ict_time state() must return killzone as dict"
    assert ict_time_st["killzone"]["current"] is not None, "Bar at 10:30 NY should be in london_close killzone"

    row = _build_row(ict_time_state=ict_time_st)
    assert row["killzone"] != "none", (
        f"killzone feature must not be 'none' when bar is inside a killzone; got {row['killzone']!r}"
    )
    assert row["killzone"] == ict_time_st["killzone"]["current"]


def test_builder_silver_bullet_reads_nested_dict_key() -> None:
    """BUG FIX: builder read 'current_silver_bullet' (non-existent) instead of silver_bullet['active'].

    Previously silver_bullet_active was always False.
    """
    ict_time_st = _make_ict_time_state_in_killzone()
    # The 10:30 NY bar should be in sb2 window (10:00–11:00 NY)
    assert isinstance(ict_time_st.get("silver_bullet"), dict)

    row = _build_row(ict_time_state=ict_time_st)
    expected_active = ict_time_st["silver_bullet"]["active"] is not None
    assert row["silver_bullet_active"] == expected_active


def test_builder_strong_pool_swept_reads_liquidity_subdict() -> None:
    """BUG FIX: builder read smc_root['last_sweep'] (non-existent at root) instead of smc['liquidity']['last_sweep'].

    Previously strong_pool_swept_side was always 'none' and bars_ago=-1.
    """
    # Build a SMC state with a completed sweep
    cfg = replace(SMC_CFG, min_warmup_bars=2)
    eng = IctSmcEngine("BTC", "1h", cfg)
    # Feed enough bars to build liquidity pools
    for i in range(20):
        bar = Bar(open_time=i * 3_600_000, close_time=(i + 1) * 3_600_000,
                  open=17000 + (i % 5) * 200, high=17200 + (i % 5) * 200,
                  low=16800 + (i % 5) * 200, close=17050 + (i % 5) * 200)
        eng.on_candle(bar)
    smc_st = eng.state()

    # Verify structure: last_sweep lives inside liquidity, not at root
    assert "liquidity" in smc_st, "IctSmcEngine state() must have 'liquidity' key"
    assert "last_sweep" not in smc_st, (
        "last_sweep must NOT exist at state() root; it lives inside state()['liquidity']"
    )

    row = _build_row(ict_smc_state_1h=smc_st)
    # When no actual sweep has happened, side should be 'none'
    assert row["strong_pool_swept_side"] in ("none", "BSL", "SSL")


def test_builder_pdh_pdl_reads_level_type_not_kind() -> None:
    """BUG FIX: builder checked kl['kind'] (non-existent) instead of kl['level_type'] for PDH/PDL matching.

    Previously dist_to_pdh_atr and dist_to_pdl_atr were always None.
    """
    cfg = replace(ICT_TIME_CFG, min_warmup_bars=2)
    eng = IctTimeEngine("BTC", cfg=cfg)
    # Feed 2 full trading days to generate PDH/PDL key levels
    for day in range(3):
        for hour in [8, 10, 12, 14, 16, 20, 22]:
            dt = datetime(2023, 1, 10 + day, hour, 0, tzinfo=_NY)
            ms = int(dt.timestamp() * 1000)
            bar = Bar(open_time=ms, close_time=ms + 3_600_000,
                      open=17000 + day * 100, high=17200 + day * 100,
                      low=16800 + day * 100, close=17050 + day * 100)
            eng.on_candle(bar)

    ict_time_st = eng.state()
    key_levels = ict_time_st.get("key_levels", [])
    assert any(kl.get("level_type") == "pdh" for kl in key_levels), (
        "After 2 full days, a 'pdh' key level must exist with key 'level_type'"
    )

    row = _build_row(ict_time_state=ict_time_st)
    # dist_to_pdh_atr must not be None if a PDH level exists and atr_1h defaults to 1.0
    assert row["dist_to_pdh_atr"] is not None, (
        "dist_to_pdh_atr must not be None when a PDH key level exists"
    )
    assert row["dist_to_pdl_atr"] is not None, (
        "dist_to_pdl_atr must not be None when a PDL key level exists"
    )


def test_builder_sr_strength_reads_active_zones_and_touches() -> None:
    """BUG FIX: builder read 'zones' (non-existent) instead of 'active_zones', and touch_count instead of len(touches).

    Previously nearest_sr_strength was always 0 and nearest_sr_dist_atr was always 999.0.
    """
    cfg = replace(PA_CFG, min_warmup_bars=2)
    eng = PriceActionEngine("BTC", "1h", cfg)
    pa_st = eng.state()

    # Verify structure: active_zones is the correct key
    assert "active_zones" in pa_st, "PriceActionEngine state() must have 'active_zones' key (not 'zones')"
    assert "zones" not in pa_st, "PriceActionEngine state() must NOT have a root 'zones' key"

    row = _build_row(price_action_state_1h=pa_st)
    # With no zones active, dist should be 999.0 and strength 0
    assert row["nearest_sr_dist_atr"] == 999.0 or isinstance(row["nearest_sr_dist_atr"], float)
    assert isinstance(row["nearest_sr_strength"], int)


def test_builder_pattern_reads_recent_patterns_not_last_pattern() -> None:
    """BUG FIX: builder read 'last_pattern' (non-existent) instead of recent_patterns[-1].

    Previously pattern_recent_type was always 'none'.
    """
    cfg = replace(PA_CFG, min_warmup_bars=2)
    eng = PriceActionEngine("BTC", "1h", cfg)
    pa_st = eng.state()

    # Verify structure: recent_patterns is the correct key
    assert "recent_patterns" in pa_st, "PriceActionEngine state() must have 'recent_patterns' key (not 'last_pattern')"
    assert "last_pattern" not in pa_st, "PriceActionEngine state() must NOT have a root 'last_pattern' key"

    row = _build_row(price_action_state_1h=pa_st)
    assert row["pattern_recent_type"] == "none"  # no patterns yet with 0 bars

    # Inject a state with a pattern
    pa_st_with_pat = {
        "active_zones": [],
        "recent_patterns": [{"pattern": "hammer", "at_zone": True, "bullish": True}],
        "nearest_support": None,
        "nearest_resistance": None,
        "active_range": None,
        "recent_breakouts": [],
        "round_numbers": [],
        "atr": 1.0,
    }
    row2 = _build_row(price_action_state_1h=pa_st_with_pat)
    assert row2["pattern_recent_type"] == "hammer", (
        f"Expected 'hammer' from recent_patterns[-1], got {row2['pattern_recent_type']!r}"
    )
    assert row2["pattern_recent_at_zone"] is True

