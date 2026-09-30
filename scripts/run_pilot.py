"""Deskmind Step 10 Pilot Execution Script.

Runs ONLY BTCUSDT on TRAIN split subset (2023-01-01 -> 2023-03-31).
Phase A: Agent history + Feature Store
Phase B: H1-H5 Event Studies
Phase C: H1 Train Backtest
Reports exact measured runtimes, disk usage, signal counts, and full plan extrapolation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import os
from pathlib import Path
import time
from typing import Any

from deskmind.agents.ict_smc.agent import SymbolIctSmcAgent
from deskmind.agents.ict_time.agent import IctTimeAgent
from deskmind.agents.price_action.agent import PriceActionAgent
from deskmind.agents.trend.agent import SymbolTrendAgent
from deskmind.agents.trend.models import Bar
from deskmind.confluence.engine import evaluate_confluence
from deskmind.data.bulk_download import fetch_month
from deskmind.data.datasets import Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.features.builder import build_feature_row
from deskmind.features.store import FeatureStore
from deskmind.research.event_study import calculate_event_study, generate_matched_random_baseline
from deskmind.research.gates import evaluate_research_gates
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin


async def download_symbol_klines(symbol: str, year: int, month: int, tf: str) -> list[Bar]:
    """Download monthly verified klines from data.binance.vision."""
    ds = Dataset(kind="klines", timeframe=tf)
    async with RateLimitedClient("https://data.binance.vision") as client:
        table = await fetch_month(client, symbol, ds, year, month)
        if table is None:
            return []
        
        dict_data = table.to_pydict()
        open_times = dict_data["open_time"]
        close_times = dict_data["close_time"]
        opens = dict_data["open"]
        highs = dict_data["high"]
        lows = dict_data["low"]
        closes = dict_data["close"]
        volumes = dict_data["volume"]

        def _ms(v: Any) -> int:
            if isinstance(v, datetime):
                return int(v.timestamp() * 1000)
            return int(v)

        bars = [
            Bar(
                open_time=_ms(open_times[i]),
                close_time=_ms(close_times[i]),
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
            )
            for i in range(len(open_times))
        ]
        return bars


async def main():
    print("=========================================================")
    print(" DESKMIND STEP 10 PILOT RUN (BTCUSDT: 2023-01 -> 2023-03)")
    print("=========================================================")
    
    # ---------------------------------------------------------------- PHASE 0: Data Fetch
    t0_data = time.perf_counter()
    print("\n[Phase 0] Downloading verified Binance Vision klines for BTCUSDT (2022-12 -> 2023-03)...")
    
    bars_by_tf: dict[str, list[Bar]] = {"5m": [], "15m": [], "1h": [], "2h": []}
    # Download 2022-12 for agent warmup (288 5m bars = 1 day minimum, 1 month downloaded)
    for year, month in [(2022, 12), (2023, 1), (2023, 2), (2023, 3)]:
        for tf in ["5m", "15m", "1h", "2h"]:
            b_list = await download_symbol_klines("BTCUSDT", year, month, tf)
            bars_by_tf[tf].extend(b_list)
            
    # Sort and deduplicate
    for tf in bars_by_tf:
        bars_by_tf[tf] = sorted({b.open_time: b for b in bars_by_tf[tf]}.values(), key=lambda b: b.open_time)

    t_data_duration = time.perf_counter() - t0_data
    print(f"[OK] Data ready in {t_data_duration:.2f}s. Counts: 5m={len(bars_by_tf['5m'])}, 15m={len(bars_by_tf['15m'])}, 1h={len(bars_by_tf['1h'])}, 2h={len(bars_by_tf['2h'])}")

    # ---------------------------------------------------------------- PHASE A: Feature Store
    t0_feature = time.perf_counter()
    print("\n[Phase A] Running 4 Agents & Building 5m Feature Store...")

    trend_2h = SymbolTrendAgent("BTCUSDT", ["2h"])
    trend_1h = SymbolTrendAgent("BTCUSDT", ["1h"])
    trend_15m = SymbolTrendAgent("BTCUSDT", ["15m"])
    trend_5m = SymbolTrendAgent("BTCUSDT", ["5m"])

    ict_smc_1h = SymbolIctSmcAgent("BTCUSDT", ["1h"])
    ict_smc_15m = SymbolIctSmcAgent("BTCUSDT", ["15m"])
    ict_smc_5m = SymbolIctSmcAgent("BTCUSDT", ["5m"])

    ict_time = IctTimeAgent("BTCUSDT")

    pa_agent = PriceActionAgent("BTCUSDT")

    feature_store_dir = Path("data/features_pilot")
    feature_store = FeatureStore(base_dir=feature_store_dir)

    feature_rows = []
    pilot_start_ms = int(datetime(2023, 1, 1, 0, 0, tzinfo=UTC).timestamp() * 1000)
    pilot_end_ms = int(datetime(2023, 3, 31, 23, 59, tzinfo=UTC).timestamp() * 1000)

    # Align 5m bars
    bars_5m = bars_by_tf["5m"]
    bars_15m = {b.close_time: b for b in bars_by_tf["15m"]}
    bars_1h = {b.close_time: b for b in bars_by_tf["1h"]}
    bars_2h = {b.close_time: b for b in bars_by_tf["2h"]}

    for b5 in bars_5m:
        ct = b5.close_time
        # Feed candle to 5m engines
        trend_5m.on_bar_close(ct, {"5m": [b5]}, with_report=False)
        ict_smc_5m.on_bar_close(ct, {"5m": [b5]}, with_report=False)
        ict_time.on_bar_close(ct, {"5m": [b5]}, with_report=False)
        pa_agent.on_bar_close(ct, {"5m": [b5]}, with_report=False)

        if ct in bars_15m:
            b15 = bars_15m[ct]
            trend_15m.on_bar_close(ct, {"15m": [b15]}, with_report=False)
            ict_smc_15m.on_bar_close(ct, {"15m": [b15]}, with_report=False)

        if ct in bars_1h:
            b1 = bars_1h[ct]
            trend_1h.on_bar_close(ct, {"1h": [b1]}, with_report=False)
            ict_smc_1h.on_bar_close(ct, {"1h": [b1]}, with_report=False)
            pa_agent.on_bar_close(ct, {"1h": [b1]}, with_report=False)

        if ct in bars_2h:
            b2 = bars_2h[ct]
            trend_2h.on_bar_close(ct, {"2h": [b2]}, with_report=False)

        # Record features ONLY within pilot start -> end
        if pilot_start_ms <= ct <= pilot_end_ms:
            ts_ms = ct
            st_t2h = trend_2h.engines["2h"].state()
            st_t1h = trend_1h.engines["1h"].state()
            st_t15m = trend_15m.engines["15m"].state()
            st_t5m = trend_5m.engines["5m"].state()

            st_smc1h = ict_smc_1h.engines["1h"].state()
            st_smc15m = ict_smc_15m.engines["15m"].state()
            st_smc5m = ict_smc_5m.engines["5m"].state()

            st_time = ict_time.engine.state()

            st_pa1h = pa_agent.engines["1h"].state() if "1h" in pa_agent.engines else None
            st_pa5m = pa_agent.engines["5m"].state() if "5m" in pa_agent.engines else None

            row = build_feature_row(
                symbol="BTCUSDT",
                timestamp=ts_ms,
                trend_state_2h=st_t2h,
                trend_state_1h=st_t1h,
                trend_state_15m=st_t15m,
                trend_state_5m=st_t5m,
                ict_smc_state_1h=st_smc1h,
                ict_smc_state_15m=st_smc15m,
                ict_smc_state_5m=st_smc5m,
                ict_time_state=st_time,
                price_action_state_1h=st_pa1h,
                price_action_state_5m=st_pa5m,
                current_close=b5.close,
            )
            feature_rows.append(row)

    feature_store.write_features(feature_rows)
    t_feature_duration = time.perf_counter() - t0_feature

    # Calculate disk usage
    dir_size_bytes = sum(f.stat().st_size for f in feature_store_dir.glob("**/*") if f.is_file())
    disk_used_mb = dir_size_bytes / (1024 * 1024)

    print(f"[OK] Feature store built in {t_feature_duration:.2f}s. Total rows: {len(feature_rows)}, Disk used: {disk_used_mb:.2f} MB")

    # ---------------------------------------------------------------- PHASE B: Event Studies
    t0_event = time.perf_counter()
    print("\n[Phase B] Evaluating Hypotheses H1-H5 & Event Studies...")

    plugins = [
        H1DiscountOBPlugin(),
        H2SweepReversalPlugin(),
        H3SilverBulletPlugin(),
        H4BreakoutRetestPlugin(),
        H5PDSweepPlugin(),
    ]

    stored_rows = feature_store.read_features("BTCUSDT", split="train")
    bars_by_ts = {b.close_time: b for b in bars_5m}
    all_5m_list = sorted(bars_5m, key=lambda b: b.close_time)

    signal_counts: dict[str, int] = {}
    es_results_by_hyp: dict[str, list[Any]] = {}

    for p in plugins:
        signals = []
        for r in stored_rows:
            sig = p.evaluate(r, current_close=r["position_pct_1h"] or 25000.0)
            if sig:
                signals.append(sig)

        signal_counts[p.hypothesis_id] = len(signals)

        # Calculate forward event studies
        es_list = []
        for sig in signals:
            fwd_bars = [b for b in all_5m_list if sig.timestamp < b.close_time <= sig.timestamp + 48 * 300_000]
            if fwd_bars:
                res = calculate_event_study(sig, fwd_bars)
                es_list.append(res)
        es_results_by_hyp[p.hypothesis_id] = es_list

    t_event_duration = time.perf_counter() - t0_event
    print(f"[OK] Event studies completed in {t_event_duration:.2f}s.")
    for p in plugins:
        cnt = signal_counts[p.hypothesis_id]
        print(f"  - {p.hypothesis_id} ({p.name}): {cnt} signals")

    # ---------------------------------------------------------------- PHASE C: H1 Train Backtest
    t0_backtest = time.perf_counter()
    print("\n[Phase C] Running H1 Train Backtest...")
    h1_es = es_results_by_hyp["H1_v1.0.0"]
    h1_baseline = generate_matched_random_baseline([e.signal for e in h1_es], {"BTCUSDT": all_5m_list})
    h1_gate = evaluate_research_gates("H1_v1.0.0", h1_es, h1_baseline, trial_count=1)
    t_backtest_duration = time.perf_counter() - t0_backtest

    print(f"[OK] H1 Train backtest & gate evaluation completed in {t_backtest_duration:.2f}s.")
    print(f"  - Gate Passed: {h1_gate.passed}")
    print(f"  - Trades: {h1_gate.trades_count}, Gross Exp: {h1_gate.gross_expectancy_r}R, Net Exp: {h1_gate.net_expectancy_r}R")

    # ---------------------------------------------------------------- SUMMARY & EXTRAPOLATION
    print("\n=========================================================")
    print(" PILOT RUN MEASURED RESULTS & FULL PLAN EXTRAPOLATION")
    print("=========================================================")
    print(f"Measured Pilot Scope: 1 Symbol (BTCUSDT) x 3 Months (2023-01 -> 2023-03)")
    print(f"  - Phase 0 Data Download:    {t_data_duration:.2f}s")
    print(f"  - Phase A Feature Store:    {t_feature_duration:.2f}s (Disk: {disk_used_mb:.2f} MB)")
    print(f"  - Phase B Event Studies:    {t_event_duration:.2f}s")
    print(f"  - Phase C H1 Backtest:      {t_backtest_duration:.2f}s")
    print(f"  - Total Pilot Time:        {t_data_duration + t_feature_duration + t_event_duration + t_backtest_duration:.2f}s")

    # Extrapolation for Full Plan: 10 symbols x 2 years (2023-01 -> 2024-12 = 24 months) = 80x pilot scale
    scale_factor = (10 * 24) / (1 * 3)  # 80x
    extrap_data_min = (t_data_duration * scale_factor) / 60
    extrap_feature_min = (t_feature_duration * scale_factor) / 60 / 4  # 4 parallel threads
    extrap_event_min = (t_event_duration * scale_factor) / 60
    extrap_backtest_min = (t_backtest_duration * scale_factor) / 60
    extrap_disk_gb = (disk_used_mb * scale_factor) / 1024
    total_extrap_min = extrap_data_min + extrap_feature_min + extrap_event_min + extrap_backtest_min

    print("\n--- Full Plan Extrapolation (Universe Top 10, 2023-2024: 80x Pilot) ---")
    print(f"  - Estimated Data Fetch:     {extrap_data_min:.1f} minutes")
    print(f"  - Estimated Feature Store:  {extrap_feature_min:.1f} minutes (4 workers)")
    print(f"  - Estimated Event Studies:  {extrap_event_min:.1f} minutes")
    print(f"  - Estimated Train Backtests:{extrap_backtest_min:.1f} minutes")
    print(f"  - Estimated Disk Usage:     {extrap_disk_gb:.2f} GB")
    print(f"  - Total Full Plan Runtime:  {total_extrap_min:.1f} minutes (~{total_extrap_min/60:.2f} hours)")
    print("=========================================================")


if __name__ == "__main__":
    asyncio.run(main())
