"""Unit tests for Strategy Lab multi-timeframe support (STEP 1, 2, 3)."""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from deskmind.data.resample_mtf import resample_dataframe
from deskmind.strategy_lab.pipeline import (
    TF_ORDER,
    export_comparison_report_csv,
    get_comparison_report,
)
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.workers import scan_symbol_worker

FIXTURE_2000_BARS = Path("tests/fixtures/ethusdt_4h_2000.parquet")
TAS_STRATEGY_PATH = Path("src/deskmind/strategy_lab/strategies/tas_v1.py")


def test_resample_mtf_drops_incomplete_buckets():
    """Verify that 30m requires 2 x 15m and 2h requires 2 x 1h; incomplete buckets are dropped."""
    # 4 x 15m candles: 00:00, 00:15, 00:30, 00:45
    # Let's remove 00:15 so the 00:00-00:30 bucket has only 1 candle!
    times = [
        pd.Timestamp("2024-01-01 00:00:00", tz="UTC"),
        # 00:15 dropped!
        pd.Timestamp("2024-01-01 00:30:00", tz="UTC"),
        pd.Timestamp("2024-01-01 00:45:00", tz="UTC"),
    ]
    df = pd.DataFrame({
        "open_time": times,
        "open": [100.0, 102.0, 103.0],
        "high": [105.0, 104.0, 106.0],
        "low": [99.0, 101.0, 102.0],
        "close": [102.0, 103.0, 105.0],
        "volume": [10.0, 12.0, 15.0],
        "quote_volume": [1000.0, 1200.0, 1500.0],
        "count": [100, 120, 150],
        "taker_buy_volume": [5.0, 6.0, 7.5],
        "taker_buy_quote_volume": [500.0, 600.0, 750.0],
    })

    # Resample to 30m
    df_30m = resample_dataframe(df, bucket_freq="30min", expected_sub_candles=2)
    # The 00:00 bucket only had 1 candle, so it MUST be dropped!
    # The 00:30 bucket had 2 candles (00:30 and 00:45), so it is kept!
    assert len(df_30m) == 1
    assert df_30m.iloc[0]["open_time"] == pd.Timestamp("2024-01-01 00:30:00", tz="UTC")
    assert df_30m.iloc[0]["open"] == 102.0
    assert df_30m.iloc[0]["high"] == 106.0
    assert df_30m.iloc[0]["low"] == 101.0
    assert df_30m.iloc[0]["close"] == 105.0
    assert df_30m.iloc[0]["volume"] == 27.0


def test_resample_mtf_aggregation_correctness():
    """Verify OHLCV aggregation matches expected math."""
    times = [
        pd.Timestamp("2024-01-01 00:00:00", tz="UTC"),
        pd.Timestamp("2024-01-01 01:00:00", tz="UTC"),
        pd.Timestamp("2024-01-01 02:00:00", tz="UTC"),
        pd.Timestamp("2024-01-01 03:00:00", tz="UTC"),
    ]
    df = pd.DataFrame({
        "open_time": times,
        "open": [100.0, 105.0, 110.0, 108.0],
        "high": [108.0, 112.0, 115.0, 114.0],
        "low": [98.0, 104.0, 107.0, 106.0],
        "close": [105.0, 110.0, 108.0, 112.0],
        "volume": [100.0, 200.0, 150.0, 250.0],
        "quote_volume": [10000.0, 20000.0, 15000.0, 25000.0],
        "count": [10, 20, 15, 25],
        "taker_buy_volume": [50.0, 100.0, 75.0, 125.0],
        "taker_buy_quote_volume": [5000.0, 10000.0, 7500.0, 12500.0],
    })

    df_2h = resample_dataframe(df, bucket_freq="2h", expected_sub_candles=2)
    assert len(df_2h) == 2
    # First 2h bucket: 00:00
    b0 = df_2h.iloc[0]
    assert b0["open_time"] == pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
    assert b0["open"] == 100.0
    assert b0["high"] == 112.0
    assert b0["low"] == 98.0
    assert b0["close"] == 110.0
    assert b0["volume"] == 300.0
    assert b0["quote_volume"] == 30000.0

    # Second 2h bucket: 02:00
    b1 = df_2h.iloc[1]
    assert b1["open_time"] == pd.Timestamp("2024-01-01 02:00:00", tz="UTC")
    assert b1["open"] == 110.0
    assert b1["high"] == 115.0
    assert b1["low"] == 106.0
    assert b1["close"] == 112.0
    assert b1["volume"] == 400.0


def test_single_process_vs_multiprocess_parity():
    """Verify process-level parallelism produces IDENTICAL results to single-process run."""
    symbols = ["ETHUSDT", "BTCUSDT"]
    args = (
        "4h",
        str(TAS_STRATEGY_PATH),
        "2023-01-01",
        "2024-12-31",
        20_000_000.0,
        True,
        "data/daily_quote_volume.parquet",
    )

    # 1. Single-process execution
    single_res = [scan_symbol_worker(sym, *args) for sym in symbols]

    # 2. Multi-process execution
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(scan_symbol_worker, sym, *args) for sym in symbols]
        multi_res = [f.result() for f in futures]

    assert len(single_res) == len(multi_res)
    for s_item, m_item in zip(single_res, multi_res):
        assert s_item["symbol"] == m_item["symbol"]
        assert s_item["is_eligible"] == m_item["is_eligible"]
        assert s_item["positions"] == m_item["positions"]
        assert s_item["closed_trades"] == m_item["closed_trades"]
        assert s_item["net_profit"] == m_item["net_profit"]
        assert s_item["profit_factor"] == m_item["profit_factor"]
        assert s_item["mean_r"] == m_item["mean_r"]
        assert s_item["avg_cost_in_r"] == m_item["avg_cost_in_r"]
        assert s_item["positions_records"] == m_item["positions_records"]


def test_trial_k_allocation_per_job(tmp_path):
    """Each selected timeframe = a separate job = 1 trial in K."""
    reg = StrategyRegistry(
        registry_file=tmp_path / "registry.json",
        uploads_dir=tmp_path / "uploads",
    )
    initial_k = reg.trial_count_k
    k1 = reg.allocate_trial_k()
    k2 = reg.allocate_trial_k()
    k3 = reg.allocate_trial_k()
    assert k1 == initial_k + 1
    assert k2 == initial_k + 2
    assert k3 == initial_k + 3
    assert reg.trial_count_k == k3


def test_comparison_report_csv_format():
    """Verify export_comparison_report_csv outputs the exact 12 columns required in STEP 3."""
    csv_str = export_comparison_report_csv()
    header_line = csv_str.strip().split("\n")[0]
    expected_headers = [
        "TF",
        "eligible coins",
        "TRAIN positions (all eligible)",
        "TRAIN pooled mean R (all eligible)",
        "TRAIN pooled PF (all eligible)",
        "avg cost in R",
        "selected",
        "2025 positions",
        "2025 pooled mean R",
        "2025 pooled PF",
        "% selected profitable 2025",
        "verdict",
    ]
    assert header_line == ",".join(expected_headers)
