"""Full TRAIN research pipeline runner.

Orchestrates Phase 0 (Universe & Data Step), Phase A (Feature Store Build with Quality Gate),
Phase B (Event Studies vs Matched Random Baseline), and Phase C (Real Engine Backtests & Gates)
across the top 10 point-in-time universe symbols on the TRAIN split (2023-01-01 -> 2024-12-31).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import logging
from pathlib import Path
import subprocess
import time
from typing import Any

import pyarrow.parquet as pq

from deskmind.agents.ict_smc.agent import SymbolIctSmcAgent
from deskmind.agents.ict_smc.config import DEFAULT_CONFIG as SMC_CFG
from deskmind.agents.ict_time.agent import IctTimeAgent
from deskmind.agents.ict_time.config import DEFAULT_CONFIG as ICT_TIME_CFG
from deskmind.agents.price_action.agent import PriceActionAgent
from deskmind.agents.price_action.config import DEFAULT_CONFIG as PA_CFG
from deskmind.agents.trend.agent import SymbolTrendAgent
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.models import Bar
from deskmind.backtest.engine import BacktestResult
from deskmind.backtest.runner import run_backtest
from deskmind.config import UNIVERSE_HISTORICAL_CANDIDATES, get_settings
from deskmind.data.bulk_download import VISION_BASE_URL, fetch_month
from deskmind.data.datasets import Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.storage import Store, load_klines, write_parquet
from deskmind.features.builder import build_feature_row
from deskmind.features.quality import check_feature_quality, feature_quality_report
from deskmind.features.store import FeatureStore
from deskmind.research.event_study import calculate_event_study, generate_matched_random_baseline
from deskmind.research.gates import evaluate_research_gates
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin
from deskmind.research.split_guard import SplitGuard
from deskmind.research.universe_selector import select_point_in_time_symbols
from deskmind.universe.history import (
    MembershipHistory,
    compute_membership,
    fetch_daily_volumes,
)
from deskmind.universe.ranking import UniverseConfig
from deskmind.universe.store import PostgresUniverseStore


logger = logging.getLogger("deskmind.research.train_run")


def get_current_git_commit() -> str:
    """Retrieve HEAD git commit hash."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "UNKNOWN"


def _load_checkpoint(checkpoint_file: Path) -> dict[str, Any] | None:
    if not checkpoint_file.exists():
        return None
    try:
        return json.loads(checkpoint_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_checkpoint(checkpoint: dict[str, Any], checkpoint_file: Path) -> None:
    checkpoint["updated_at"] = datetime.now(UTC).isoformat()
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = checkpoint_file.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    tmp_path.replace(checkpoint_file)


def _log_msg(msg: str, log_file: Path | None = None) -> None:
    print(msg)
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


async def _download_binance_vision_month(
    symbol: str, year: int, month: int, tf: str, data_dir: Path | str | None = None
) -> list[Bar]:
    """Download monthly verified klines from Binance Vision, caching to local parquet if data_dir provided."""
    ds = Dataset(kind="klines", timeframe=tf)
    if data_dir:
        store = Store(Path(data_dir), symbol)
        part_path = store.partition_path(ds, year, month)
        if part_path.exists():
            tbl = pq.read_table(part_path)
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

    async with RateLimitedClient(VISION_BASE_URL, max_concurrency=8) as client:
        table = await fetch_month(client, symbol, ds, year, month)
        if table is None:
            return []

        if data_dir:
            try:
                store = Store(Path(data_dir), symbol)
                part_path = store.partition_path(ds, year, month)
                write_parquet(part_path, table)
            except Exception as e:
                logger.warning("Could not cache parquet partition for %s %d-%02d: %s", symbol, year, month, e)

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
    """Load verified bars from local DuckDB/Parquet store."""
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


async def run_full_train_pipeline(
    split: str = "train",
    start_date: str = "2023-01-01",
    end_date: str = "2024-12-31",
    top_n: int = 10,
    output_dir: str = "data/features",
    log_file_path: str = "reports/research/train_run.log",
    checkpoint_path: str = "reports/research/train_checkpoint.json",
    data_dir: str = "data",
    resume: bool = False,
) -> dict[str, Any]:
    """Run full research training pipeline across top 10 point-in-time universe symbols."""
    # ------------------------------------------------------------------ GUARD & LOG SETUP
    SplitGuard.validate_split_request(split=split, hypothesis_id="H1_v1.0.0")

    current_commit = get_current_git_commit()
    checkpoint_file = Path(checkpoint_path)

    log_file = Path(log_file_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if resume and log_file.exists() else "w"
    with open(log_file, mode, encoding="utf-8") as f:
        action = "RESUMED" if resume else "STARTED"
        f.write(f"\n=== DESKMIND FULL TRAIN RUN {action} AT {datetime.now(UTC).isoformat()} ===\n")

    _log_msg(f"Target Split: {split.upper()} [{start_date} -> {end_date}]", log_file)
    _log_msg(f"Scope: Top {top_n} point-in-time universe symbols with 30-day warm-up", log_file)
    _log_msg(f"Git Commit: {current_commit}", log_file)

    if resume:
        checkpoint = _load_checkpoint(checkpoint_file)
        if checkpoint is None:
            _log_msg(f"  [Resume] No existing checkpoint found at {checkpoint_file}. Initializing fresh checkpoint.", log_file)
            checkpoint = {
                "git_commit": current_commit,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "split": split,
                "start_date": start_date,
                "end_date": end_date,
                "top_n": top_n,
                "phases": {},
            }
            _save_checkpoint(checkpoint, checkpoint_file)
        else:
            cp_commit = checkpoint.get("git_commit")
            if cp_commit != current_commit:
                err_msg = (
                    f"Refusing to resume train run across different code versions:\n"
                    f"  Checkpoint Git Commit: {cp_commit}\n"
                    f"  Current Git Commit   : {current_commit}\n"
                    f"Train run outputs must never be mixed across git commits. "
                    f"Start a clean run without --resume, or revert code to {cp_commit}."
                )
                _log_msg(f"ERROR: {err_msg}", log_file)
                raise RuntimeError(err_msg)

            if (
                checkpoint.get("split") != split
                or checkpoint.get("start_date") != start_date
                or checkpoint.get("end_date") != end_date
            ):
                err_msg = (
                    f"Refusing to resume: Run configuration mismatch with checkpoint:\n"
                    f"  Checkpoint: split={checkpoint.get('split')}, {checkpoint.get('start_date')} -> {checkpoint.get('end_date')}\n"
                    f"  Requested : split={split}, {start_date} -> {end_date}"
                )
                _log_msg(f"ERROR: {err_msg}", log_file)
                raise RuntimeError(err_msg)

            _log_msg(f"  [Resume] Checkpoint verified from {checkpoint_file} (commit {current_commit[:8]}).", log_file)
    else:
        checkpoint = {
            "git_commit": current_commit,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "split": split,
            "start_date": start_date,
            "end_date": end_date,
            "top_n": top_n,
            "phases": {},
        }
        _save_checkpoint(checkpoint, checkpoint_file)

    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=UTC)
    train_start_ms = int(start_dt.timestamp() * 1000)
    train_end_ms = int(end_dt.timestamp() * 1000)

    # ------------------------------------------------------------------ PHASE 0: Universe & Dry-Run
    phases = checkpoint.setdefault("phases", {})
    phase0_cp = phases.get("phase_0", {})
    t0_phase0 = time.perf_counter()

    if resume and phase0_cp.get("status") == "COMPLETED" and phase0_cp.get("pit_symbols"):
        pit_symbols = phase0_cp["pit_symbols"]
        _log_msg(f"\n[Phase 0] SKIPPED (Resumed: {len(pit_symbols)} PIT symbols verified): {pit_symbols}", log_file)
    else:
        _log_msg("\n[Phase 0] Replaying point-in-time liquidity universe ranking (2023-01-01 -> 2024-12-31)...", log_file)
        universe_candidates = list(UNIVERSE_HISTORICAL_CANDIDATES)

        async with RateLimitedClient(VISION_BASE_URL, max_concurrency=10) as vision_client:
            v_tasks = [
                fetch_daily_volumes(vision_client, s, date(2022, 6, 1), date(2024, 12, 31))
                for s in universe_candidates
            ]
            res_v = await asyncio.gather(*v_tasks)
            volumes = dict(zip(universe_candidates, res_v))

        settings = get_settings()
        cfg = UniverseConfig.from_settings(settings)
        membership_rows = compute_membership(volumes, start_dt.date(), end_dt.date(), cfg)
        universe_history = MembershipHistory(membership_rows)

        try:
            pg_store = PostgresUniverseStore()
            await pg_store.save_membership(membership_rows, source="train_run")
            _log_msg(f"  Saved {len(membership_rows):,} universe membership rows to database.", log_file)
        except Exception as e:
            _log_msg(f"  [Notice] DB save skipped: {e}", log_file)

        pit_symbols = select_point_in_time_symbols(
            universe_history, start_dt.date(), end_dt.date(), top_n=top_n
        )
        _log_msg(f"  Point-in-time Top {top_n} Universe Union ({len(pit_symbols)} symbols): {pit_symbols}", log_file)

        phases["phase_0"] = {
            "status": "COMPLETED",
            "completed_at": datetime.now(UTC).isoformat(),
            "pit_symbols": pit_symbols,
            "membership_rows_count": len(membership_rows),
        }
        _save_checkpoint(checkpoint, checkpoint_file)
        _log_msg("  [Checkpoint] Phase 0 completed and saved to checkpoint.", log_file)

    # Dry-Run Size Estimate
    _log_msg("\n--- DRY-RUN DATA & STORAGE ESTIMATE ---", log_file)
    total_months = 25  # 2022-12 through 2024-12
    est_kline_rows_per_sym = 105120 * 2 + 8928  # ~219,168 5m bars
    total_est_rows = est_kline_rows_per_sym * len(pit_symbols)
    est_raw_mb_per_sym = total_months * 5.8
    total_est_mb = est_raw_mb_per_sym * len(pit_symbols)
    _log_msg(f"  Symbols in Universe Union   : {len(pit_symbols)}", log_file)
    _log_msg(f"  Warm-up Period Included     : 2022-12-02 -> 2022-12-31 (30 days)", log_file)
    _log_msg(f"  TRAIN Split Window          : 2023-01-01 -> 2024-12-31 (731 days)", log_file)
    _log_msg(f"  Estimated 5m Bars Total     : ~{total_est_rows:,} bars", log_file)
    _log_msg(f"  Estimated Compressed Size   : ~{total_est_mb:.1f} MB (~{total_est_mb / 1024:.2f} GB)", log_file)
    _log_msg(f"  Phase 0 Completed in {time.perf_counter() - t0_phase0:.2f}s", log_file)

    # ------------------------------------------------------------------ DATA PREPARATION
    t0_data = time.perf_counter()
    _log_msg("\n[Phase 0b] Loading verified klines (warm-up: 2022-12 + train: 2023-2024)...", log_file)
    all_bars_by_symbol: dict[str, dict[str, list[Bar]]] = {}

    for sym in pit_symbols:
        _log_msg(f"  Preparing data for {sym}...", log_file)
        sym_bars: dict[str, list[Bar]] = {"5m": [], "15m": [], "1h": [], "2h": []}

        # 1. Warm-up month (2022-12)
        for tf in ("5m", "15m", "1h", "2h"):
            w_bars = await _download_binance_vision_month(sym, 2022, 12, tf, data_dir=data_dir)
            sym_bars[tf].extend(w_bars)

        # 2. Train window bars (from local store or vision download if missing)
        for tf in ("5m", "15m", "1h", "2h"):
            stored = _load_stored_bars(data_dir, sym, tf, start_dt, end_dt)
            if stored:
                sym_bars[tf].extend(stored)
            else:
                _log_msg(f"    Fetching {sym} {tf} from Binance Vision...", log_file)
                for yr in (2023, 2024):
                    for mo in range(1, 13):
                        m_bars = await _download_binance_vision_month(sym, yr, mo, tf, data_dir=data_dir)
                        sym_bars[tf].extend(m_bars)

        # Sort and deduplicate
        for tf in sym_bars:
            sym_bars[tf] = sorted(
                {b.open_time: b for b in sym_bars[tf]}.values(), key=lambda b: b.open_time
            )

        all_bars_by_symbol[sym] = sym_bars
        _log_msg(f"    -> {sym} ready: 5m={len(sym_bars['5m']):,}, 1h={len(sym_bars['1h']):,}", log_file)

    phases["phase_0b"] = {
        "status": "COMPLETED",
        "completed_at": datetime.now(UTC).isoformat(),
        "symbols": {
            sym: {tf: len(all_bars_by_symbol[sym][tf]) for tf in ("5m", "1h")}
            for sym in pit_symbols
        },
    }
    _save_checkpoint(checkpoint, checkpoint_file)
    _log_msg(f"  [OK] Data preparation finished in {time.perf_counter() - t0_data:.2f}s [Checkpoint saved].", log_file)

    # ------------------------------------------------------------------ PHASE A: Feature Store Build
    t0_feat = time.perf_counter()
    _log_msg("\n[Phase A] Running 4 incremental agents & constructing feature store...", log_file)
    feature_dir = Path(output_dir)
    store = FeatureStore(base_dir=feature_dir)
    all_feature_rows: list[Any] = []
    row_dicts_by_symbol: dict[str, list[dict[str, Any]]] = {}

    phase_a_cp = phases.setdefault(
        "phase_a", {"status": "IN_PROGRESS", "symbols_completed": [], "symbol_counts": {}}
    )
    completed_syms = set(phase_a_cp.get("symbols_completed", []))

    for sym in pit_symbols:
        # Check if symbol already completed and verify output
        if resume and sym in completed_syms:
            _log_msg(f"  Verifying existing feature store output for {sym}...", log_file)
            stored_dicts = store.read_features(symbol=sym, split=split)
            if stored_dicts:
                try:
                    check_feature_quality(stored_dicts)
                    _log_msg(
                        f"    -> [Resume] {sym} features verified ({len(stored_dicts):,} rows, Quality Gate PASS) -> SKIPPED.",
                        log_file,
                    )
                    row_dicts_by_symbol[sym] = stored_dicts
                    all_feature_rows.extend(stored_dicts)
                    continue
                except Exception as e:
                    _log_msg(f"    -> [Warning] {sym} stored feature verification failed: {e}. Recomputing...", log_file)

        _log_msg(f"  Building features for {sym}...", log_file)
        trend_cfg = TrendConfig(min_warmup_bars=24)
        trend_2h = SymbolTrendAgent(sym, ["2h"], trend_cfg)
        trend_1h = SymbolTrendAgent(sym, ["1h"], trend_cfg)
        trend_15m = SymbolTrendAgent(sym, ["15m"], trend_cfg)
        trend_5m = SymbolTrendAgent(sym, ["5m"], trend_cfg)

        ict_smc_1h = SymbolIctSmcAgent(sym, ["1h"], SMC_CFG, trend_cfg)
        ict_smc_15m = SymbolIctSmcAgent(sym, ["15m"], SMC_CFG, trend_cfg)
        ict_smc_5m = SymbolIctSmcAgent(sym, ["5m"], SMC_CFG, trend_cfg)

        ict_time = IctTimeAgent(sym, ICT_TIME_CFG)
        pa_agent = PriceActionAgent(sym, PA_CFG)

        sym_bars_5m = all_bars_by_symbol[sym]["5m"]
        sym_bars_15m = {b.close_time: b for b in all_bars_by_symbol[sym]["15m"]}
        sym_bars_1h = {b.close_time: b for b in all_bars_by_symbol[sym]["1h"]}
        sym_bars_2h = {b.close_time: b for b in all_bars_by_symbol[sym]["2h"]}

        sym_feature_rows = []

        for b5 in sym_bars_5m:
            ct = b5.close_time

            swings_1h: list[dict[str, Any]] = []
            if ct in sym_bars_1h:
                b1 = sym_bars_1h[ct]
                t1_out = trend_1h.on_bar_close(ct, {"1h": [b1]}, with_report=False)
                swings_1h = [
                    item for tf, item in t1_out.changes if item.get("kind") in ("swing_high", "swing_low")
                ]
                ict_smc_1h.on_bar_close(ct, {"1h": [b1]}, with_report=False)
                pa_agent.on_bar_close(ct, {"1h": [b1]}, swings_by_tf={"1h": swings_1h}, with_report=False)

            if ct in sym_bars_15m:
                b15 = sym_bars_15m[ct]
                trend_15m.on_bar_close(ct, {"15m": [b15]}, with_report=False)
                ict_smc_15m.on_bar_close(ct, {"15m": [b15]}, with_report=False)

            if ct in sym_bars_2h:
                b2 = sym_bars_2h[ct]
                trend_2h.on_bar_close(ct, {"2h": [b2]}, with_report=False)

            t5_out = trend_5m.on_bar_close(ct, {"5m": [b5]}, with_report=False)
            swings_5m = [
                item for tf, item in t5_out.changes if item.get("kind") in ("swing_high", "swing_low")
            ]
            ict_smc_5m.on_bar_close(ct, {"5m": [b5]}, with_report=False)
            ict_time.on_bar_close(ct, {"5m": [b5]}, with_report=False)
            pa_agent.on_bar_close(ct, {"5m": [b5]}, swings_by_tf={"5m": swings_5m}, with_report=False)

            # Emit rows for TRAIN split window only
            if train_start_ms <= ct <= train_end_ms:
                row = build_feature_row(
                    symbol=sym,
                    timestamp=ct,
                    trend_state_2h=trend_2h.engines["2h"].state(),
                    trend_state_1h=trend_1h.engines["1h"].state(),
                    trend_state_15m=trend_15m.engines["15m"].state(),
                    trend_state_5m=trend_5m.engines["5m"].state(),
                    ict_smc_state_1h=ict_smc_1h.engines["1h"].state(),
                    ict_smc_state_15m=ict_smc_15m.engines["15m"].state(),
                    ict_smc_state_5m=ict_smc_5m.engines["5m"].state(),
                    ict_time_state=ict_time.engine.state(),
                    price_action_state_1h=pa_agent.engines["1h"].state() if "1h" in pa_agent.engines else None,
                    price_action_state_5m=pa_agent.engines["5m"].state() if "5m" in pa_agent.engines else None,
                    current_close=b5.close,
                )
                sym_feature_rows.append(row)

        # Quality Check on symbol features
        sym_dicts = [r.as_dict() for r in sym_feature_rows]
        check_feature_quality(sym_dicts)
        store.write_features(sym_feature_rows)

        row_dicts_by_symbol[sym] = sym_dicts
        all_feature_rows.extend(sym_dicts)

        if sym not in phase_a_cp["symbols_completed"]:
            phase_a_cp["symbols_completed"].append(sym)
        phase_a_cp.setdefault("symbol_counts", {})[sym] = len(sym_feature_rows)
        _save_checkpoint(checkpoint, checkpoint_file)
        _log_msg(
            f"    -> {sym} features built: {len(sym_feature_rows):,} rows [Quality Gate PASS] "
            f"[Checkpoint saved {len(phase_a_cp['symbols_completed'])}/{len(pit_symbols)}]",
            log_file,
        )

    phase_a_cp["status"] = "COMPLETED"
    phase_a_cp["completed_at"] = datetime.now(UTC).isoformat()
    _save_checkpoint(checkpoint, checkpoint_file)
    t_feat_duration = time.perf_counter() - t0_feat
    _log_msg(f"  [OK] Feature Store built in {t_feat_duration:.2f}s ({len(all_feature_rows):,} total rows) [Checkpoint saved].", log_file)

    # ------------------------------------------------------------------ PHASE B: Event Studies
    t0_es = time.perf_counter()
    _log_msg("\n[Phase B] Evaluating Pre-registered Hypotheses H1-H5 & Event Studies...", log_file)

    plugins: list[HypothesisPlugin] = [
        H1DiscountOBPlugin(),
        H2SweepReversalPlugin(),
        H3SilverBulletPlugin(),
        H4BreakoutRetestPlugin(),  # H4_v1.1.0 with nearest_sr_touches >= 3
        H5PDSweepPlugin(),
    ]

    all_row_dicts: list[dict[str, Any]] = []
    for s_rows in row_dicts_by_symbol.values():
        all_row_dicts.extend(s_rows)

    close_maps: dict[str, dict[int, float]] = {
        sym: {b.close_time: b.close for b in all_bars_by_symbol[sym]["5m"]}
        for sym in pit_symbols
    }

    setups_by_hyp: dict[str, list[Any]] = {}
    es_by_hyp: dict[str, list[Any]] = {}
    baseline_by_hyp: dict[str, list[Any]] = {}
    bars_passing_by_hyp: dict[str, int] = {}

    for p in plugins:
        hid = p.hypothesis_id
        unique_signals = []
        seen_setup_ids = set()
        bars_passing = 0

        for r in all_row_dicts:
            sym = r["symbol"]
            c = close_maps[sym].get(r["timestamp"], 25000.0)
            sig = p.evaluate(r, current_close=c)
            if sig:
                bars_passing += 1
                sid = sig.setup_id or f"{sig.symbol}:{sig.timestamp}:{sig.direction}"
                if sid not in seen_setup_ids:
                    seen_setup_ids.add(sid)
                    unique_signals.append(sig)

        setups_by_hyp[hid] = unique_signals
        bars_passing_by_hyp[hid] = bars_passing

        # Event Study
        study_bars_5m = {sym: all_bars_by_symbol[sym]["5m"] for sym in pit_symbols}
        es_results = calculate_event_study(unique_signals, study_bars_5m, holding_bars=(1, 4, 12, 48))
        es_by_hyp[hid] = es_results

        # Matched Random Baseline (1,000 runs)
        baseline = generate_matched_random_baseline(
            [e.signal for e in es_results], study_bars_5m, samples=1000, holding_bars=(1, 4, 12, 48)
        )
        baseline_by_hyp[hid] = baseline
        _log_msg(f"  {hid} ({p.name}): {bars_passing:,} bars -> {len(unique_signals):,} unique setups evaluated.", log_file)

    phases["phase_b"] = {
        "status": "COMPLETED",
        "completed_at": datetime.now(UTC).isoformat(),
        "bars_passing": bars_passing_by_hyp,
        "unique_setups": {hid: len(setups_by_hyp[hid]) for hid in setups_by_hyp},
    }
    _save_checkpoint(checkpoint, checkpoint_file)
    _log_msg(f"  [OK] Event studies and baselines completed in {time.perf_counter() - t0_es:.2f}s [Checkpoint saved].", log_file)

    # ------------------------------------------------------------------ PHASE C: Real Engine Backtests
    t0_bt = time.perf_counter()
    _log_msg("\n[Phase C] Executing Real Engine Backtests & Gate Verification...", log_file)

    feature_lookup: dict[str, dict[str, Any]] = {}
    for r in all_row_dicts:
        sym = r["symbol"]
        ts = int(r["timestamp"])
        open_ms = ((ts - 1) // 300_000) * 300_000
        feature_lookup[f"{sym}:{ts}"] = r
        feature_lookup[f"{sym}:{ts + 1}"] = r
        feature_lookup[f"{sym}:{ts - 1}"] = r
        feature_lookup[f"{sym}:{open_ms}"] = r

    strategy_names = {
        "H1_v1.0.0": "h1_discount_ob",
        "H2_v1.0.0": "h2_sweep_reversal",
        "H3_v1.0.0": "h3_silver_bullet",
        "H4_v1.1.0": "h4_breakout_retest",
        "H5_v1.0.0": "h5_pd_sweep",
    }

    trial_counts = {
        "H1_v1.0.0": 1,
        "H2_v1.0.0": 1,
        "H3_v1.0.0": 1,
        "H4_v1.1.0": 2,  # Trial #1 was v1.0.0 (VOID)
        "H5_v1.0.0": 1,
    }

    results_by_hyp: dict[str, Any] = {}
    phase_c_cp = phases.setdefault("phase_c", {"status": "IN_PROGRESS", "hypotheses_completed": {}})

    for p in plugins:
        hid = p.hypothesis_id
        strat_name = strategy_names[hid]
        _log_msg(f"\n--- Running BacktestEngine for {hid} ({strat_name}) ---", log_file)

        bt_res = await run_backtest(
            strategy_name=strat_name,
            split=split,
            symbols=pit_symbols,
            start_date=start_date,
            end_date=end_date,
            warmup_days=30,
            strategy_params={"feature_lookup": feature_lookup},
            data_dir=Path(data_dir),
        )
        engine_res: BacktestResult = bt_res["result"]
        metrics = bt_res["metrics"]

        # Gate Evaluation
        es = es_by_hyp[hid]
        base = baseline_by_hyp[hid]
        gate = evaluate_research_gates(
            hid, es, base, trial_count=trial_counts[hid], backtest_result=engine_res
        )

        results_by_hyp[hid] = {
            "plugin": p,
            "engine_result": engine_res,
            "metrics": metrics,
            "gate": gate,
            "es_results": es,
            "baseline": base,
        }
        phase_c_cp["hypotheses_completed"][hid] = {
            "trades": len(engine_res.trades),
            "raw_signals": engine_res.raw_entry_signals,
            "gate_verdict": gate.verdict,
            "deflated_sharpe": float(gate.deflated_sharpe),
        }
        _save_checkpoint(checkpoint, checkpoint_file)
        _log_msg(f"  {hid} Engine: {len(engine_res.trades)} trades, raw signals: {engine_res.raw_entry_signals}, Gate: {gate.verdict} [Checkpoint saved]", log_file)

    phase_c_cp["status"] = "COMPLETED"
    phase_c_cp["completed_at"] = datetime.now(UTC).isoformat()
    _save_checkpoint(checkpoint, checkpoint_file)

    t_bt_duration = time.perf_counter() - t0_bt
    _log_msg(f"\n[OK] All research backtests completed in {t_bt_duration:.2f}s.", log_file)

    # ------------------------------------------------------------------ COMPILE FINAL COMPREHENSIVE REPORT
    _log_msg("\n" + "=" * 90, log_file)
    _log_msg(" DESKMIND RESEARCH FRAMEWORK — FULL TRAIN REPORT (2023-01-01 -> 2024-12-31)", log_file)
    _log_msg("=" * 90, log_file)

    # 1. Funnel Table
    _log_msg("\n### 1. Condition Funnels & Setup Deduplication (All Symbols)", log_file)
    _log_msg(f"{'Hypothesis':<12} {'Bars Passing':>14} {'% Bars':>10} {'Unique Setups':>15} {'Trades (Max 1 Pos)':>20}", log_file)
    _log_msg("-" * 75, log_file)
    for p in plugins:
        hid = p.hypothesis_id
        b_pass = bars_passing_by_hyp[hid]
        setups = len(setups_by_hyp[hid])
        trades = len(results_by_hyp[hid]["engine_result"].trades)
        pct_bars = (b_pass / len(all_feature_rows)) * 100
        _log_msg(f"{hid:<12} {b_pass:>14,} {pct_bars:>9.2f}% {setups:>15,} {trades:>20,}", log_file)

    # 2. Performance & Gates per Hypothesis
    for p in plugins:
        hid = p.hypothesis_id
        res = results_by_hyp[hid]
        eng: BacktestResult = res["engine_result"]
        gate = res["gate"]
        es = res["es_results"]
        base = res["baseline"]

        _log_msg("\n" + "=" * 90, log_file)
        _log_msg(f" HYPOTHESIS: {hid} — {p.name}", log_file)
        _log_msg("=" * 90, log_file)

        # Overview Table
        trades_count = len(eng.trades)
        wins = [t for t in eng.trades if t.pnl > Decimal("0")]
        win_rate = (len(wins) / trades_count * 100) if trades_count > 0 else 0.0

        r_mults = [float(t.r_multiple) for t in eng.trades]
        gross_exp = (sum(r_mults) / len(r_mults)) if r_mults else 0.0
        tot_friction = float(eng.total_fees_paid + eng.total_slippage_paid + eng.total_funding_paid)
        cost_per_trade_r = (tot_friction / trades_count / 100.0) if trades_count > 0 else 0.0
        net_exp = gross_exp - cost_per_trade_r

        _log_msg(f"  Trial Count in Registry     : {trial_counts[hid]}", log_file)
        _log_msg(f"  Unique Setups Emitted       : {len(setups_by_hyp[hid]):,}", log_file)
        _log_msg(f"  Real Engine Trades Executed : {trades_count:,}", log_file)
        _log_msg(f"  Win Rate                    : {win_rate:.1f}% ({len(wins)} / {trades_count})", log_file)
        _log_msg(f"  Gross Expectancy            : {gross_exp:.3f}R", log_file)
        _log_msg(f"  Frictional Cost per Trade   : {cost_per_trade_r:.3f}R", log_file)
        _log_msg(f"  Net Expectancy              : {net_exp:.3f}R", log_file)
        _log_msg(f"  Max Drawdown                : {float(gate.max_drawdown_pct):.2f}%", log_file)
        _log_msg(f"  Deflated Sharpe Ratio (DSR) : {float(gate.deflated_sharpe):.3f}", log_file)
        _log_msg(f"  Random Baseline Percentile  : {float(gate.random_baseline_pct):.1f}%", log_file)

        # Train Halves Consistency
        mid_ms = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
        h1_trades = [t for t in eng.trades if t.entry_time_ms < mid_ms]
        h2_trades = [t for t in eng.trades if t.entry_time_ms >= mid_ms]
        h1_r = sum(float(t.r_multiple) for t in h1_trades) / len(h1_trades) if h1_trades else 0.0
        h2_r = sum(float(t.r_multiple) for t in h2_trades) / len(h2_trades) if h2_trades else 0.0
        _log_msg(f"  Train Half 1 (2023)         : {len(h1_trades)} trades, {h1_r:+.3f}R avg", log_file)
        _log_msg(f"  Train Half 2 (2024)         : {len(h2_trades)} trades, {h2_r:+.3f}R avg", log_file)

        # Rejections by Reason
        rej_counts = {}
        for rej in eng.rejected_intents:
            code = rej.reasons[0] if rej.reasons else "UNKNOWN"
            rej_counts[code] = rej_counts.get(code, 0) + 1
        _log_msg("\n  Risk Manager Rejections:", log_file)
        if rej_counts:
            for code, cnt in sorted(rej_counts.items(), key=lambda x: -x[1]):
                _log_msg(f"    - {code:<25}: {cnt:>5}", log_file)
        else:
            _log_msg("    (None)", log_file)

        # Per-Symbol Results (symbols with >= 10 trades)
        sym_trades: dict[str, list[Any]] = {}
        for t in eng.trades:
            sym_trades.setdefault(t.symbol, []).append(t)
        _log_msg("\n  Per-Symbol Performance (>= 10 Trades):", log_file)
        qualified_symbols = {s: tr for s, tr in sym_trades.items() if len(tr) >= 10}
        if qualified_symbols:
            _log_msg(f"    {'Symbol':<10} {'Trades':>8} {'Win Rate':>10} {'Gross PnL ($)':>14} {'Avg R':>8}", log_file)
            _log_msg("    " + "-" * 54, log_file)
            for s, trs in sorted(qualified_symbols.items(), key=lambda x: -len(x[1])):
                w_cnt = sum(1 for t in trs if t.pnl > Decimal("0"))
                wr = (w_cnt / len(trs)) * 100
                tot_pnl = sum(float(t.pnl) for t in trs)
                avg_r = sum(float(t.r_multiple) for t in trs) / len(trs)
                _log_msg(f"    {s:<10} {len(trs):>8} {wr:>9.1f}% {tot_pnl:>14.2f} {avg_r:>8.2f}R", log_file)
        else:
            _log_msg("    (No single symbol had >= 10 trades; positions rotated across universe)", log_file)

        # Event Study Forward Horizon Table vs Matched Random
        _log_msg("\n  Event Study MFE/MAE Horizons vs Random Baseline:", log_file)
        _log_msg(f"    {'Horizon':<10} {'Hypothesis MFE':>16} {'Random MFE':>14} {'Hypothesis MAE':>16} {'Random MAE':>14}", log_file)
        _log_msg("    " + "-" * 74, log_file)
        for h in (1, 4, 12, 48):
            hyp_mfe = sum(e.mfe.get(h, 0.0) for e in es) / len(es) if es else 0.0
            hyp_mae = sum(e.mae.get(h, 0.0) for e in es) / len(es) if es else 0.0
            rnd_mfe = sum(b.mfe.get(h, 0.0) for b in base) / len(base) if base else 0.0
            rnd_mae = sum(b.mae.get(h, 0.0) for b in base) / len(base) if base else 0.0
            _log_msg(f"    {h:<3} bars   {hyp_mfe:>15.2f}% {rnd_mfe:>13.2f}% {hyp_mae:>15.2f}% {rnd_mae:>13.2f}%", log_file)

        # Gate Checklist
        _log_msg("\n  Gate Checklist (TRAIN -> VALIDATION):", log_file)
        for crit, passed in gate.checklist.items():
            status = "PASS" if passed else "FAIL"
            _log_msg(f"    [{status:<4}] {crit}", log_file)
        _log_msg(f"\n  >>> FINAL GATE VERDICT: {gate.verdict.upper()} <<<", log_file)

    _log_msg("\n" + "=" * 90, log_file)
    _log_msg(f"=== FULL TRAIN RUN COMPLETED SUCCESSFULLY IN {time.perf_counter() - t0_phase0:.2f}s ===", log_file)
    _log_msg("=" * 90 + "\n", log_file)

    return results_by_hyp
