"""Deterministic research pilot runner for BTCUSDT (TRAIN split subset: 2023-01-01 -> 2023-03-31).

Executes:
Phase 0: Verified market data loading (warm-up + pilot window)
Phase A: Incremental agent execution, point-in-time feature store build & quality gate
Phase B: Pre-registered hypotheses (H1-H5) condition funnels, signal counts & event studies
Phase C: H1 real backtest simulation & research gate evaluation
Reporting: Measured runtimes, condition funnels, signal counts, feature statistics & full-plan extrapolation
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Any

from deskmind.agents.ict_smc.agent import SymbolIctSmcAgent
from deskmind.agents.ict_smc.config import DEFAULT_CONFIG as SMC_CFG
from deskmind.agents.ict_time.agent import IctTimeAgent
from deskmind.agents.ict_time.config import DEFAULT_CONFIG as ICT_TIME_CFG
from deskmind.agents.price_action.agent import PriceActionAgent
from deskmind.agents.price_action.config import DEFAULT_CONFIG as PA_CFG
from deskmind.agents.trend.agent import SymbolTrendAgent
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.models import Bar
from deskmind.data.bulk_download import fetch_month
from deskmind.data.datasets import Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.storage import load_klines
from deskmind.features.builder import build_feature_row
from deskmind.features.quality import check_feature_quality, feature_quality_report
from deskmind.features.store import FeatureStore
from deskmind.research.event_study import calculate_event_study, generate_matched_random_baseline
from deskmind.research.gates import evaluate_research_gates
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin
from deskmind.research.split_guard import SplitGuard


async def _download_binance_vision_month(symbol: str, year: int, month: int, tf: str) -> list[Bar]:
    """Download monthly verified klines from Binance Vision for warm-up."""
    ds = Dataset(kind="klines", timeframe=tf)
    async with RateLimitedClient("https://data.binance.vision") as client:
        table = await fetch_month(client, symbol, ds, year, month)
        if table is None:
            return []

        pyd = table.to_pydict()
        open_times = pyd["open_time"]
        close_times = pyd["close_time"]
        opens = pyd["open"]
        highs = pyd["high"]
        lows = pyd["low"]
        closes = pyd["close"]

        def _ms(v: Any) -> int:
            if isinstance(v, datetime):
                return int(v.timestamp() * 1000)
            return int(v)

        return [
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


def _load_stored_bars(data_dir: Path | str, symbol: str, tf: str, start: datetime, end: datetime) -> list[Bar]:
    """Load verified bars from the local store using DuckDB/Parquet."""
    tbl = load_klines(data_dir, symbol, tf, start, end)
    if tbl is None:
        return []
    pyd = tbl.to_pydict()
    open_times = pyd["open_time"]
    close_times = pyd["close_time"]
    opens = pyd["open"]
    highs = pyd["high"]
    lows = pyd["low"]
    closes = pyd["close"]

    def _ms(v: Any) -> int:
        if isinstance(v, datetime):
            return int(v.timestamp() * 1000)
        return int(v)

    return [
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


async def run_pilot_pipeline(
    symbol: str | None = None,
    split: str = "train",
    start_date: str = "2023-01-01",
    end_date: str = "2023-03-31",
    output_dir: str = "data/features_pilot",
    data_dir: str = "data",
) -> dict[str, Any]:
    """Run the complete research pilot pipeline via CLI."""
    symbol = symbol or ("BTC" + "USDT")
    print("=" * 70)
    print(f" DESKMIND RESEARCH PILOT: {symbol} [{start_date} -> {end_date}] (Split: {split})")
    print("=" * 70)

    # 0. Enforce Split Guard
    SplitGuard.validate_split_request(split=split)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, microsecond=999000, tzinfo=UTC)
    pilot_start_ms = int(start_dt.timestamp() * 1000)
    pilot_end_ms = int(end_dt.timestamp() * 1000)

    # ------------------------------------------------------------------ PHASE 0: Data Preparation
    t0_data = time.perf_counter()
    print("\n[Phase 0] Preparing verified klines (warm-up: 2022-12 + pilot window: 2023-01 -> 2023-03)...")

    bars_by_tf: dict[str, list[Bar]] = {"5m": [], "15m": [], "1h": [], "2h": []}

    # Warmup month 2022-12 downloaded from Binance Vision
    for tf in ("5m", "15m", "1h", "2h"):
        warmup_bars = await _download_binance_vision_month(symbol, 2022, 12, tf)
        bars_by_tf[tf].extend(warmup_bars)

    # Pilot window loaded from stored Parquet dataset
    for tf in ("5m", "15m", "1h", "2h"):
        stored_bars = _load_stored_bars(data_dir, symbol, tf, start_dt, end_dt)
        bars_by_tf[tf].extend(stored_bars)

    # Sort & deduplicate
    for tf in bars_by_tf:
        bars_by_tf[tf] = sorted({b.open_time: b for b in bars_by_tf[tf]}.values(), key=lambda b: b.open_time)

    t_data_duration = time.perf_counter() - t0_data
    print(f"  [OK] Data ready in {t_data_duration:.2f}s.")
    print(f"       Bars: 5m={len(bars_by_tf['5m'])}, 15m={len(bars_by_tf['15m'])}, 1h={len(bars_by_tf['1h'])}, 2h={len(bars_by_tf['2h'])}")

    # ------------------------------------------------------------------ PHASE A: Feature Store Build
    t0_feature = time.perf_counter()
    print("\n[Phase A] Running 4 incremental agents & constructing feature store...")

    trend_cfg = TrendConfig(min_warmup_bars=24)
    trend_2h = SymbolTrendAgent(symbol, ["2h"], trend_cfg)
    trend_1h = SymbolTrendAgent(symbol, ["1h"], trend_cfg)
    trend_15m = SymbolTrendAgent(symbol, ["15m"], trend_cfg)
    trend_5m = SymbolTrendAgent(symbol, ["5m"], trend_cfg)

    ict_smc_1h = SymbolIctSmcAgent(symbol, ["1h"], SMC_CFG, trend_cfg)
    ict_smc_15m = SymbolIctSmcAgent(symbol, ["15m"], SMC_CFG, trend_cfg)
    ict_smc_5m = SymbolIctSmcAgent(symbol, ["5m"], SMC_CFG, trend_cfg)

    ict_time = IctTimeAgent(symbol, ICT_TIME_CFG)
    pa_agent = PriceActionAgent(symbol, PA_CFG)

    feature_dir = Path(output_dir)
    store = FeatureStore(base_dir=feature_dir)

    bars_5m = bars_by_tf["5m"]
    bars_15m = {b.close_time: b for b in bars_by_tf["15m"]}
    bars_1h = {b.close_time: b for b in bars_by_tf["1h"]}
    bars_2h = {b.close_time: b for b in bars_by_tf["2h"]}

    feature_rows = []

    for b5 in bars_5m:
        ct = b5.close_time

        # Higher timeframes
        swings_1h: list[dict[str, Any]] = []
        if ct in bars_1h:
            b1 = bars_1h[ct]
            t1_out = trend_1h.on_bar_close(ct, {"1h": [b1]}, with_report=False)
            swings_1h = [item for tf, item in t1_out.changes if item.get("kind") in ("swing_high", "swing_low")]
            ict_smc_1h.on_bar_close(ct, {"1h": [b1]}, with_report=False)
            pa_agent.on_bar_close(ct, {"1h": [b1]}, swings_by_tf={"1h": swings_1h}, with_report=False)

        if ct in bars_15m:
            b15 = bars_15m[ct]
            trend_15m.on_bar_close(ct, {"15m": [b15]}, with_report=False)
            ict_smc_15m.on_bar_close(ct, {"15m": [b15]}, with_report=False)

        if ct in bars_2h:
            b2 = bars_2h[ct]
            trend_2h.on_bar_close(ct, {"2h": [b2]}, with_report=False)

        # Feed 5m candle
        t5_out = trend_5m.on_bar_close(ct, {"5m": [b5]}, with_report=False)
        swings_5m = [item for tf, item in t5_out.changes if item.get("kind") in ("swing_high", "swing_low")]
        ict_smc_5m.on_bar_close(ct, {"5m": [b5]}, with_report=False)
        ict_time.on_bar_close(ct, {"5m": [b5]}, with_report=False)
        pa_agent.on_bar_close(ct, {"5m": [b5]}, swings_by_tf={"5m": swings_5m}, with_report=False)

        # Only emit feature rows for pilot window
        if pilot_start_ms <= ct <= pilot_end_ms:
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
                symbol=symbol,
                timestamp=ct,
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

    # Convert to dicts for validation & write
    row_dicts = [r.as_dict() for r in feature_rows]

    # Strict Quality Check
    print("  [Quality Gate] Running strict feature quality verification...")
    check_feature_quality(row_dicts)
    quality_stats = feature_quality_report(row_dicts)

    # Write Parquet Store
    store.write_features(feature_rows)
    t_feature_duration = time.perf_counter() - t0_feature

    dir_size_bytes = sum(f.stat().st_size for f in feature_dir.glob("**/*") if f.is_file())
    disk_used_mb = dir_size_bytes / (1024 * 1024)

    print(f"  [OK] Feature store built in {t_feature_duration:.2f}s.")
    print(f"       Total rows: {len(feature_rows):,}, Disk usage: {disk_used_mb:.2f} MB")

    # ------------------------------------------------------------------ PHASE B: Hypotheses & Event Studies
    t0_event = time.perf_counter()
    print("\n[Phase B] Evaluating Pre-registered Hypotheses H1-H5...")

    plugins = [
        H1DiscountOBPlugin(),
        H2SweepReversalPlugin(),
        H3SilverBulletPlugin(),
        H4BreakoutRetestPlugin(),
        H5PDSweepPlugin(),
    ]

    funnels: dict[str, dict[str, int]] = {}
    signals_by_hyp: dict[str, list[Any]] = {}
    bars_passing_by_hyp: dict[str, int] = {}
    es_results_by_hyp: dict[str, list[Any]] = {}

    all_5m_list = sorted(bars_5m, key=lambda b: b.close_time)
    close_map = {b.close_time: b.close for b in bars_5m}

    # Initialize funnels
    total_bars = len(feature_rows)
    for p in plugins:
        funnels[p.hypothesis_id] = {"total_bars": total_bars}

    for p in plugins:
        unique_signals = []
        seen_setup_ids = set()
        bars_passing = 0
        hid = p.hypothesis_id

        for r in row_dicts:
            bar_c = close_map.get(r["timestamp"], 25000.0)
            sig = p.evaluate(r, current_close=bar_c)
            if sig:
                bars_passing += 1
                sid = sig.setup_id or f"{sig.symbol}:{sig.timestamp}:{sig.direction}"
                if sid not in seen_setup_ids:
                    seen_setup_ids.add(sid)
                    unique_signals.append(sig)

        signals_by_hyp[hid] = unique_signals
        bars_passing_by_hyp[hid] = bars_passing

        # Condition breakdown tracking
        if hid == "H1_v1.0.0":
            funnels[hid]["1_htf_trend"] = sum(1 for r in row_dicts if r["htf_trend_1h"] in ("bullish", "bearish"))
            funnels[hid]["2_pd_zone_aligned"] = sum(
                1 for r in row_dicts
                if (r["htf_trend_1h"] == "bullish" and r["pd_zone_1h"] == "discount")
                or (r["htf_trend_1h"] == "bearish" and r["pd_zone_1h"] == "premium")
            )
            funnels[hid]["3_poi_in_zone"] = sum(
                1 for r in row_dicts
                if (
                    (r["htf_trend_1h"] == "bullish" and r["pd_zone_1h"] == "discount")
                    or (r["htf_trend_1h"] == "bearish" and r["pd_zone_1h"] == "premium")
                )
                and (
                    (r["nearest_fvg_dist_atr"] <= 1.0 and r["nearest_fvg_size_atr"] >= 0.5)
                    or (r["nearest_ob_dist_atr"] <= 1.0 and r["nearest_ob_state"] == "fresh")
                )
            )
            funnels[hid]["4_5m_choch_bars"] = bars_passing
            funnels[hid]["5_unique_setups"] = len(unique_signals)

        elif hid == "H2_v1.0.0":
            funnels[hid]["1_killzone"] = sum(1 for r in row_dicts if r["killzone"] in ("london_open", "ny_am"))
            funnels[hid]["2_liquidity_sweep"] = sum(
                1 for r in row_dicts
                if r["killzone"] in ("london_open", "ny_am")
                and r["strong_pool_swept_side"] in ("SSL", "BSL")
                and 0 <= r["strong_pool_swept_bars_ago"] <= 12
            )
            funnels[hid]["3_5m_choch_bars"] = bars_passing
            funnels[hid]["4_unique_setups"] = len(unique_signals)

        elif hid == "H3_v1.0.0":
            funnels[hid]["1_silver_bullet_active"] = sum(1 for r in row_dicts if r["silver_bullet_active"])
            funnels[hid]["2_htf_trend"] = sum(1 for r in row_dicts if r["silver_bullet_active"] and r["htf_trend_1h"] in ("bullish", "bearish"))
            funnels[hid]["3_disp_fvg_bars"] = bars_passing
            funnels[hid]["4_unique_setups"] = len(unique_signals)

        elif hid == "H4_v1.0.0":
            funnels[hid]["1_sr_strength_ge_2"] = sum(1 for r in row_dicts if r["nearest_sr_strength"] >= 2 and r["nearest_sr_dist_atr"] <= 0.5)
            funnels[hid]["2_pattern_at_zone"] = sum(1 for r in row_dicts if r["nearest_sr_strength"] >= 2 and r["nearest_sr_dist_atr"] <= 0.5 and r["pattern_recent_at_zone"])
            funnels[hid]["3_pattern_aligned_bars"] = bars_passing
            funnels[hid]["4_unique_setups"] = len(unique_signals)

        elif hid == "H5_v1.0.0":
            funnels[hid]["1_killzone"] = sum(1 for r in row_dicts if r["killzone"] in ("london_open", "ny_am"))
            funnels[hid]["2_pdh_pdl_swept"] = sum(1 for r in row_dicts if r["killzone"] in ("london_open", "ny_am") and (r["pdh_swept_recent"] or r["pdl_swept_recent"]))
            funnels[hid]["3_5m_choch_bars"] = bars_passing
            funnels[hid]["4_unique_setups"] = len(unique_signals)

        # Forward Event Studies on Unique Setups
        es_list = []
        for sig in unique_signals:
            fwd_bars = [b for b in all_5m_list if sig.timestamp < b.close_time <= sig.timestamp + 48 * 300_000]
            if fwd_bars:
                res = calculate_event_study(sig, fwd_bars)
                es_list.append(res)
        es_results_by_hyp[hid] = es_list

    t_event_duration = time.perf_counter() - t0_event
    print(f"  [OK] Hypotheses and event studies evaluated in {t_event_duration:.2f}s.")

    # ------------------------------------------------------------------ PHASE C: Real Engine Backtest & Gates
    t0_backtest = time.perf_counter()
    print("\n[Phase C] Routing Research Backtest through the real BacktestEngine...")

    from deskmind.backtest.engine import BacktestResult
    from deskmind.backtest.runner import run_backtest

    feature_lookup = {}
    for r in row_dicts:
        ts = int(r["timestamp"])
        open_ms = ((ts - 1) // 300_000) * 300_000
        sym = r["symbol"]
        feature_lookup[f"{sym}:{ts}"] = r
        feature_lookup[f"{sym}:{ts + 1}"] = r
        feature_lookup[f"{sym}:{ts - 1}"] = r
        feature_lookup[f"{sym}:{open_ms}"] = r

    bt_res = await run_backtest(
        strategy_name="h1_discount_ob",
        split=split,
        symbols=[symbol],
        start_date=start_date,
        end_date=end_date,
        warmup_days=30,
        strategy_params={"feature_lookup": feature_lookup},
        data_dir=Path(data_dir),
    )
    engine_result: BacktestResult = bt_res["result"]
    h1_metrics = bt_res["metrics"]

    h1_es = es_results_by_hyp["H1_v1.0.0"]
    h1_baseline = generate_matched_random_baseline([e.signal for e in h1_es], {symbol: all_5m_list})
    h1_gate = evaluate_research_gates("H1_v1.0.0", h1_es, h1_baseline, trial_count=1)

    t_backtest_duration = time.perf_counter() - t0_backtest
    print(f"  [OK] Real BacktestEngine completed in {t_backtest_duration:.2f}s.")

    # ------------------------------------------------------------------ DETAILED REPORT
    print("\n" + "=" * 75)
    print(" CONDITION FUNNELS ACROSS PRE-REGISTERED HYPOTHESES (Bars vs Unique Setups)")
    print("=" * 75)
    for p in plugins:
        hid = p.hypothesis_id
        fn = funnels[hid]
        print(f"\n--- {hid} ({p.name}) ---")
        for k, v in fn.items():
            pct = round((v / total_bars) * 100, 2)
            print(f"  {k:<28}: {v:>6} ({pct:>5.2f}%)")
        print(f"  -> Total Bars Passing Condition : {bars_passing_by_hyp[hid]:>6} ({round(bars_passing_by_hyp[hid]/total_bars*100, 2):>5.2f}%)")
        print(f"  -> Unique Setups Emitted        : {len(signals_by_hyp[hid]):>6}")

    print("\n" + "=" * 75)
    print(" FEATURE QUALITY AUDIT (All 34 Features)")
    print("=" * 75)
    print(f"{'Feature':<28}{'Null %':>8}{'Distinct':>10}   {'Sample Values'}")
    print("-" * 75)
    for item in quality_stats:
        samples = ", ".join(str(v) for v in item["sample_values"][:4])
        print(f"{item['feature']:<28}{item['null_pct']:>7.1f}%{item['distinct_count']:>10}   [{samples}]")

    print("\n" + "=" * 75)
    print(" S/R STRENGTH SCALE & DISTRIBUTION (nearest_sr_strength)")
    print("=" * 75)
    strengths = [r["nearest_sr_strength"] for r in row_dicts]
    s_counts = {
        "0 (no zone)": sum(1 for s in strengths if s == 0),
        "1 - 19": sum(1 for s in strengths if 1 <= s < 20),
        "20 - 39": sum(1 for s in strengths if 20 <= s < 40),
        "40 - 59": sum(1 for s in strengths if 40 <= s < 60),
        "60 - 79": sum(1 for s in strengths if 60 <= s < 80),
        "80 - 100": sum(1 for s in strengths if 80 <= s <= 100),
    }
    for bucket, count in s_counts.items():
        pct = (count / len(strengths)) * 100
        print(f"  Score {bucket:<15}: {count:>6} bars ({pct:>5.1f}%)")

    print("\n" + "=" * 75)
    print(" REAL BACKTEST ENGINE EXECUTION (H1 Discount OB)")
    print("=" * 75)
    print(f"  Strategy Name               : H1 Discount OB / FVG Continuation")
    print(f"  Simulation Engine           : BacktestEngine v{engine_result.trades[0].id.split('_')[0] if engine_result.trades else '1.0.0'}")
    print(f"  Total Trades Executed       : {len(engine_result.trades)}")
    print(f"  Total Raw Entry Signals     : {engine_result.raw_entry_signals}")
    print(f"  Duplicate Setup Skips       : {engine_result.duplicate_setup_skips}")
    print(f"  Position Open Skips         : {engine_result.position_open_skips}")
    print(f"  Same-Bar SL/TP Conflicts    : {engine_result.same_bar_sl_tp_count}")
    print(f"  Liquidations Count          : {engine_result.liquidations_count}")
    print(f"\n  Cost Breakdown:")
    print(f"    - Total Fees Paid         : ${float(engine_result.total_fees_paid):,.2f}")
    print(f"    - Total Slippage Paid     : ${float(engine_result.total_slippage_paid):,.2f}")
    print(f"    - Total Funding Paid      : ${float(engine_result.total_funding_paid):,.2f}")
    total_friction = engine_result.total_fees_paid + engine_result.total_slippage_paid + engine_result.total_funding_paid
    print(f"    - Total Frictional Costs  : ${float(total_friction):,.2f}")

    print(f"\n  Rejections by Reason:")
    rej_counts = {}
    for rej in engine_result.rejected_intents:
        code = rej.reasons[0] if rej.reasons else "UNKNOWN"
        rej_counts[code] = rej_counts.get(code, 0) + 1
    if rej_counts:
        for code, count in sorted(rej_counts.items(), key=lambda x: -x[1]):
            print(f"    - {code:<25}: {count:>5}")
    else:
        print("    (None - 0 rejections)")

    print(f"\n  First 10 Trades List:")
    if engine_result.trades:
        print(f"  {'#':<3} {'Entry Time':<17} {'Exit Time':<17} {'Side':<6} {'Entry':>9} {'Exit':>9} {'PnL ($)':>10} {'PnL (%)':>8} {'R-Mult':>7} {'Exit Reason'}")
        print("  " + "-" * 98)
        for i, t in enumerate(engine_result.trades[:10], 1):
            e_dt = datetime.fromtimestamp(t.entry_time_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")
            x_dt = datetime.fromtimestamp(t.exit_time_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")
            print(f"  {i:<3} {e_dt} {x_dt} {t.side.upper():<6} {float(t.entry_price):>9.2f} {float(t.exit_price):>9.2f} {float(t.pnl):>10.2f} {float(t.pnl_pct):>7.2f}% {float(t.r_multiple):>6.2f}R {t.exit_reason}")
    else:
        print("    (No trades filled)")

    print("\n" + "=" * 75)
    print(" RESEARCH GATE EVALUATION (H1_v1.0.0 on Unique Setups)")
    print("=" * 75)
    print(f"  Trades Count (>= 100)      : {h1_gate.trades_count:>6}  [{'PASS' if h1_gate.checklist['min_trades_100'] else 'FAIL'}]")
    print(f"  Gross Expectancy (>= 0.15R): {h1_gate.gross_expectancy_r:>6.3f}R [{'PASS' if h1_gate.checklist['gross_expectancy_0_15R'] else 'FAIL'}]")
    print(f"  Net Expectancy (> 0.0R)    : {h1_gate.net_expectancy_r:>6.3f}R [{'PASS' if h1_gate.checklist['net_expectancy_positive'] else 'FAIL'}]")
    print(f"  Beats Random (>= 95th pct) : {h1_gate.baseline_percentile:>5.1f}% [{'PASS' if h1_gate.checklist['beats_random_95th_pct'] else 'FAIL'}]")
    print(f"  Positive in Both Halves    : {h1_gate.positive_in_both_halves}       [{'PASS' if h1_gate.checklist['both_halves_positive'] else 'FAIL'}]")
    print(f"  Gate Verdict               : {'PASSED (Qualified for Validation)' if h1_gate.passed else 'FAILED (Blocked from Validation)'}")

    # Extrapolation
    total_time = t_data_duration + t_feature_duration + t_event_duration + t_backtest_duration
    scale_factor = (10 * 24) / (1 * 3)  # 80x: 10 symbols x 24 months vs 1 symbol x 3 months
    extrap_data_min = (t_data_duration * scale_factor) / 60
    extrap_feature_min = (t_feature_duration * scale_factor) / 60 / 4  # 4 threads
    extrap_event_min = (t_event_duration * scale_factor) / 60
    extrap_backtest_min = (t_backtest_duration * scale_factor) / 60
    extrap_disk_gb = (disk_used_mb * scale_factor) / 1024

    print("\n" + "=" * 75)
    print(" MEASURED RUNTIMES & FULL PLAN EXTRAPOLATION (80x Scope)")
    print("=" * 75)
    print(f"  Measured Pilot Runtime:")
    print(f"    - Phase 0 (Data Preparation) : {t_data_duration:.2f}s")
    print(f"    - Phase A (Feature Store)    : {t_feature_duration:.2f}s (Disk: {disk_used_mb:.2f} MB)")
    print(f"    - Phase B (Event Studies)    : {t_event_duration:.2f}s")
    print(f"    - Phase C (Real Engine BT)   : {t_backtest_duration:.2f}s")
    print(f"    - Total Pilot Runtime        : {total_time:.2f}s")
    print(f"\n  Extrapolated Full Plan (Top 10 symbols x 2 years 2023-2024):")
    print(f"    - Data Download & Verify     : {extrap_data_min:.1f} minutes")
    print(f"    - Feature Store (4 workers)  : {extrap_feature_min:.1f} minutes")
    print(f"    - Event Studies & Baselines  : {extrap_event_min:.1f} minutes")
    print(f"    - Real Engine Backtests      : {extrap_backtest_min:.1f} minutes")
    print(f"    - Estimated Storage Volume   : {extrap_disk_gb:.2f} GB")
    tot_est_min = extrap_data_min + extrap_feature_min + extrap_event_min + extrap_backtest_min
    print(f"    - Total Estimated Wall-Clock : {tot_est_min:.1f} minutes (~{tot_est_min/60:.2f} hours)")
    print("=" * 75)

    return {
        "status": "success",
        "symbol": symbol,
        "rows_count": len(feature_rows),
        "disk_used_mb": disk_used_mb,
        "runtimes": {
            "data": t_data_duration,
            "feature": t_feature_duration,
            "event": t_event_duration,
            "backtest": t_backtest_duration,
            "total": total_time,
        },
        "signal_counts": {hid: len(sigs) for hid, sigs in signals_by_hyp.items()},
        "bars_passing_counts": bars_passing_by_hyp,
        "h1_gate": h1_gate.as_dict(),
        "engine_trades_count": len(engine_result.trades),
        "engine_total_fees": float(engine_result.total_fees_paid),
        "engine_total_slippage": float(engine_result.total_slippage_paid),
        "engine_total_funding": float(engine_result.total_funding_paid),
    }

