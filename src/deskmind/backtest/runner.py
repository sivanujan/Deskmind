"""Orchestrator for backtest execution.

Handles:
- Split validation & test split locking
- Backtest engine execution
- Metrics & baseline calculations
- Report generation (HTML, Markdown, CSV)
- Overlay generation for chart integration
- Audit record registration in DB and registry file
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from deskmind.agents.trend.config import AGENT_VERSION
from deskmind.backtest.baselines import compute_buy_and_hold, run_random_baseline
from deskmind.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    EquityPoint,
    RejectedIntent,
    Trade,
)
from deskmind.backtest.metrics import calculate_metrics
from deskmind.backtest.registry import (
    get_git_commit,
    save_run_record_db,
    save_run_record_local,
    validate_split_access,
)
from deskmind.backtest.reporting import generate_reports
from deskmind.config import (
    BACKTEST_ENGINE_VERSION,
    BACKTEST_SPLITS,
    DEFAULT_INITIAL_CASH,
    get_settings,
)
from deskmind.risk.breakers import CircuitBreakerTracker
from deskmind.risk.config import RiskConfig
from deskmind.risk.sizer import RiskManagerSizer
from deskmind.strategies.base import BaseStrategy
from deskmind.strategies.hypothesis_base import (
    H1DiscountOBStrategy,
    H1V2VariantAStrategy,
    H1V2VariantBStrategy,
    H1V2VariantCStrategy,
    H2SweepReversalStrategy,
    H3SilverBulletStrategy,
    H4BreakoutRetestStrategy,
    H5PDSweepStrategy,
    H6TrendBreakoutStrategy,
)
from deskmind.strategies.trend_follow_v1 import TrendFollowV1
from deskmind.universe.history import MembershipHistory

logger = logging.getLogger("deskmind.backtest.runner")

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "trend_follow_v1": TrendFollowV1,
    "h1_discount_ob": H1DiscountOBStrategy,
    "h1_v2_variant_a": H1V2VariantAStrategy,
    "h1_v2_variant_b": H1V2VariantBStrategy,
    "h1_v2_variant_c": H1V2VariantCStrategy,
    "h2_sweep_reversal": H2SweepReversalStrategy,
    "h3_silver_bullet": H3SilverBulletStrategy,
    "h4_breakout_retest": H4BreakoutRetestStrategy,
    "h5_pd_sweep": H5PDSweepStrategy,
    "h6_trend_breakout": H6TrendBreakoutStrategy,
}


def build_trade_overlays(
    run_id: str,
    trades: list[Trade],
    strategy_version: str,
    rejected_intents: list[RejectedIntent] | None = None,
) -> list[dict[str, Any]]:
    """Convert backtest trades to overlay items for the chart viewer."""
    overlays: list[dict[str, Any]] = []

    for t in trades:
        e_time = t.entry_time_ms // 1000
        x_time = t.exit_time_ms // 1000
        is_win = t.pnl > Decimal("0")
        color_win = "#22c55e"
        color_loss = "#ef4444"

        # 1. Entry marker
        overlays.append(
            {
                "id": f"entry_{t.id}",
                "type": "marker",
                "kind": "trade_entry",
                "source_agent": f"backtest:{run_id}",
                "agent_version": strategy_version,
                "schema_version": 1,
                "time": e_time,
                "position": "below" if t.side == "long" else "above",
                "shape": "arrowUp" if t.side == "long" else "arrowDown",
                "color": color_win if t.side == "long" else color_loss,
                "text": f"ENTRY {t.side.upper()} @ {float(t.entry_price):.2f}",
                "confirmed_at": e_time,
                "meta": {
                    "trade_id": t.id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "price": float(t.entry_price),
                    "qty": float(t.qty),
                },
            }
        )

        # 2. Exit marker
        overlays.append(
            {
                "id": f"exit_{t.id}",
                "type": "marker",
                "kind": "trade_exit",
                "source_agent": f"backtest:{run_id}",
                "agent_version": strategy_version,
                "schema_version": 1,
                "time": x_time,
                "position": "above" if is_win else "below",
                "shape": "square",
                "color": color_win if is_win else color_loss,
                "text": f"{t.exit_reason.upper()} ({float(t.pnl_pct):+.1f}%)",
                "confirmed_at": x_time,
                "meta": {
                    "trade_id": t.id,
                    "symbol": t.symbol,
                    "pnl": float(t.pnl),
                    "r": float(t.r_multiple),
                    "reason": t.exit_reason,
                },
            }
        )

        # 3. Trade line connecting entry to exit
        overlays.append(
            {
                "id": f"line_{t.id}",
                "type": "line",
                "kind": "trade_line",
                "source_agent": f"backtest:{run_id}",
                "agent_version": strategy_version,
                "schema_version": 1,
                "start_time": e_time,
                "start_price": float(t.entry_price),
                "end_time": x_time,
                "end_price": float(t.exit_price),
                "style": "solid",
                "color": color_win if is_win else color_loss,
                "label": f"${float(t.pnl):+,.1f}",
                "confirmed_at": x_time,
                "meta": {
                    "trade_id": t.id,
                    "symbol": t.symbol,
                    "pnl": float(t.pnl),
                    "r": float(t.r_multiple),
                },
            }
        )

        # 4. SL line
        sl_price = (
            float(t.entry_price - (t.initial_risk / t.qty))
            if t.side == "long" and t.qty > Decimal("0")
            else float(t.entry_price + (t.initial_risk / t.qty))
            if t.qty > Decimal("0")
            else float(t.entry_price)
        )
        overlays.append(
            {
                "id": f"sl_{t.id}",
                "type": "line",
                "kind": "trade_sl",
                "source_agent": f"backtest:{run_id}",
                "agent_version": strategy_version,
                "schema_version": 1,
                "start_time": e_time,
                "start_price": sl_price,
                "end_time": x_time,
                "end_price": sl_price,
                "style": "dashed",
                "color": "#ef444488",
                "label": "SL",
                "confirmed_at": e_time,
                "meta": {
                    "trade_id": t.id,
                    "symbol": t.symbol,
                },
            }
        )

    # 5. Rejected intent markers (grey circle markers with reason codes in hover metadata)
    for idx, rej in enumerate(rejected_intents or []):
        r_time = rej.timestamp_ms // 1000
        dec = rej.decision or {}
        failed_reasons = [r for r in dec.get("reasons", []) if not r.get("passed", True)]
        if failed_reasons:
            reasons_summary = "; ".join(f"[{r['code']}] {r['message']}" for r in failed_reasons)
            reason_codes = ", ".join(r["code"] for r in failed_reasons)
            first_code = failed_reasons[0]["code"]
        else:
            reasons_summary = "; ".join(rej.reasons) if rej.reasons else "REJECTED"
            reason_codes = ", ".join(rej.reasons) if rej.reasons else "REJECTED"
            first_code = rej.reasons[0] if rej.reasons else "REJECTED"

        side = "long" if "long" in rej.action else "short" if "short" in rej.action else rej.action
        overlays.append(
            {
                "id": f"rejected_{idx}_{rej.symbol}_{r_time}",
                "type": "marker",
                "kind": "intent_rejected",
                "source_agent": f"risk_manager:{run_id}",
                "agent_version": strategy_version,
                "schema_version": 1,
                "time": r_time,
                "position": "above" if side == "short" else "below",
                "shape": "circle",
                "color": "#9ca3af",
                "text": f"REJECTED: {first_code}",
                "confirmed_at": r_time,
                "meta": {
                    "symbol": rej.symbol,
                    "side": side,
                    "price": float(rej.price),
                    "reason_codes": reason_codes,
                    "reasons": reasons_summary,
                },
            }
        )

    return overlays


def _save_overlays(path: Path, overlays: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(overlays, f, indent=2)


async def run_backtest(
    strategy_name: str,
    split: str = "train",
    symbols: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    initial_cash: Decimal = DEFAULT_INITIAL_CASH,
    strategy_params: dict[str, Any] | None = None,
    unlock_test: bool = False,
    confirm_test_unlock: str | None = None,
    notes: str | None = None,
    random_seeds: int = 1000,
    warmup_days: int = 30,
    disable_circuit_breakers: bool = False,
    data_dir: Path | None = None,
    reports_dir: Path = Path("reports/backtests"),
    universe: MembershipHistory | None = None,
    universe_mode: Literal["auto", "require", "off"] = "auto",
    mode: Literal["portfolio", "per_symbol_independent"] = "portfolio",
) -> dict[str, Any]:
    settings = get_settings()
    d_dir = data_dir or settings.data_dir
    syms = symbols or list(settings.data_symbols)

    if strategy_name not in STRATEGY_REGISTRY:
        raise ValueError(
            f"Unknown strategy {strategy_name!r}. Available: {list(STRATEGY_REGISTRY.keys())}"
        )

    if mode == "per_symbol_independent" and len(syms) > 1:
        # Research mode: Each symbol runs its own backtest independently.
        # Max 1 position PER SYMBOL, 0 cross-symbol blocking.
        per_symbol_runs: dict[str, dict[str, Any]] = {}
        all_trades: list[Trade] = []
        total_orders_placed = 0
        total_missed_orders = 0
        total_rejected_intents: list[RejectedIntent] = []
        total_delisted_exits = 0
        total_universe_skips = 0

        for sym in syms:
            single_res = await run_backtest(
                strategy_name=strategy_name,
                split=split,
                symbols=[sym],
                start_date=start_date,
                end_date=end_date,
                initial_cash=initial_cash,
                strategy_params=strategy_params,
                unlock_test=unlock_test,
                confirm_test_unlock=confirm_test_unlock,
                notes=notes,
                random_seeds=random_seeds,
                warmup_days=warmup_days,
                disable_circuit_breakers=disable_circuit_breakers,
                data_dir=data_dir,
                reports_dir=reports_dir,
                universe=universe,
                universe_mode=universe_mode,
                mode="portfolio",
            )
            per_symbol_runs[sym] = single_res
            r: BacktestResult = single_res["result"]
            all_trades.extend(r.trades)
            total_orders_placed += r.orders_placed
            total_missed_orders += r.missed_limit_orders
            total_rejected_intents += r.rejected_intents
            total_delisted_exits += r.delisted_exits
            total_universe_skips += r.universe_skips

        all_trades_sorted = sorted(all_trades, key=lambda t: (t.entry_time_ms, t.exit_time_ms))
        strat_cls = STRATEGY_REGISTRY[strategy_name]
        strategy = strat_cls(strategy_params or {})

        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        cb_suffix = "_no_cb" if disable_circuit_breakers else ""
        run_id = f"{strategy.name}_{split}_independent{cb_suffix}_{timestamp_str}"

        start_time_ms = min((sr["result"].start_time_ms for sr in per_symbol_runs.values()), default=0)
        end_time_ms = max((sr["result"].end_time_ms for sr in per_symbol_runs.values()), default=0)
        tot_funding = sum((sr["result"].total_funding_paid for sr in per_symbol_runs.values()), Decimal("0"))
        tot_fees = sum((sr["result"].total_fees_paid for sr in per_symbol_runs.values()), Decimal("0"))
        tot_slip = sum((sr["result"].total_slippage_paid for sr in per_symbol_runs.values()), Decimal("0"))
        same_bar_sl_tp = sum(sr["result"].same_bar_sl_tp_count for sr in per_symbol_runs.values())
        liquidations = sum(sr["result"].liquidations_count for sr in per_symbol_runs.values())
        skipped_sig = sum(sr["result"].skipped_signals_count for sr in per_symbol_runs.values())
        pos_open_skips = sum(sr["result"].position_open_skips for sr in per_symbol_runs.values())
        dup_skips = sum(sr["result"].duplicate_setup_skips for sr in per_symbol_runs.values())
        raw_signals = sum(sr["result"].raw_entry_signals for sr in per_symbol_runs.values())

        tot_pnl = sum((t.pnl for t in all_trades_sorted), Decimal("0"))
        final_equity = initial_cash + tot_pnl

        # Construct trade-exit-based equity curve
        curve = [EquityPoint(time_ms=start_time_ms, equity=initial_cash, cash=initial_cash)]
        curr_eq = initial_cash
        for t in all_trades_sorted:
            curr_eq += t.pnl
            curve.append(EquityPoint(time_ms=t.exit_time_ms, equity=curr_eq, cash=curr_eq, position_symbol=t.symbol))
        if end_time_ms > (curve[-1].time_ms if curve else 0):
            curve.append(EquityPoint(time_ms=end_time_ms, equity=curr_eq, cash=curr_eq))

        combined_result = BacktestResult(
            trades=all_trades_sorted,
            equity_curve=curve,
            same_bar_sl_tp_count=same_bar_sl_tp,
            liquidations_count=liquidations,
            skipped_signals_count=skipped_sig,
            position_open_skips=pos_open_skips,
            duplicate_setup_skips=dup_skips,
            total_funding_paid=tot_funding,
            total_fees_paid=tot_fees,
            total_slippage_paid=tot_slip,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            initial_cash=initial_cash,
            final_equity=final_equity,
            rejected_intents=total_rejected_intents,
            delisted_exits=total_delisted_exits,
            raw_entry_signals=raw_signals,
            orders_placed=total_orders_placed,
            missed_limit_orders=total_missed_orders,
            universe_skips=total_universe_skips,
        )

        return {
            "run_id": run_id,
            "mode": "per_symbol_independent",
            "output_dir": str(reports_dir / run_id),
            "result": combined_result,
            "metrics": calculate_metrics(combined_result) if combined_result.trades else None,
            "strategy_version": strategy.version,
            "per_symbol_runs": per_symbol_runs,
        }

    # Determine date range
    if start_date and end_date:
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(end_date).replace(tzinfo=UTC)
    else:
        if split not in BACKTEST_SPLITS:
            raise ValueError(f"Unknown split {split!r}. Available: {list(BACKTEST_SPLITS.keys())}")
        s_str, e_str = BACKTEST_SPLITS[split]
        start_dt = datetime.fromisoformat(s_str).replace(tzinfo=UTC)
        end_dt = datetime.now(UTC) if e_str == "latest" else datetime.fromisoformat(e_str).replace(tzinfo=UTC)

    # Validate split access (Locked test set enforcement)
    validate_split_access(split, start_dt, end_dt, unlock_test, confirm_test_unlock)

    # Initialize strategy
    strat_cls = STRATEGY_REGISTRY[strategy_name]
    params = strategy_params or {}
    strategy = strat_cls(params)

    # Run ID
    timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    cb_suffix = "_no_cb" if disable_circuit_breakers else ""
    run_id = f"{strategy.name}_{split}{cb_suffix}_{timestamp_str}"

    # Configure Risk Manager sizer with circuit breakers enabled or disabled
    risk_cfg = RiskConfig(
        max_leverage=5,
        enable_circuit_breakers=not disable_circuit_breakers,
    )
    tracker = CircuitBreakerTracker(config=risk_cfg, initial_equity=initial_cash)
    sizer = RiskManagerSizer(config=risk_cfg, tracker=tracker)

    # Point-in-time universe (universe_membership). "auto": used when it covers the trading
    # period; "require": refuse to run without it; "off": no universe filter.
    trading_from = (start_dt + timedelta(days=warmup_days)).date()
    universe_note = "off"
    if universe is None and universe_mode != "off":
        try:
            from deskmind.universe.store import PostgresUniverseStore

            universe = await PostgresUniverseStore().load_history(trading_from, end_dt.date())
        except Exception as exc:
            universe = None
            universe_note = f"unavailable: {exc}"
    if universe is not None and universe_mode != "off":
        missing = universe.missing_days(trading_from, end_dt.date())
        if missing:
            universe_note = f"incomplete: {len(missing)} days without membership"
            universe = None
        else:
            universe_note = "point_in_time"
    else:
        universe = None
    if universe_mode == "require" and universe is None:
        raise PermissionError(
            f"universe history required but {universe_note}; build it with "
            "`deskmind universe backfill-history`"
        )

    # Execute backtest engine
    engine = BacktestEngine(
        strategy=strategy,
        symbols=syms,
        data_dir=d_dir,
        start=start_dt,
        end=end_dt,
        initial_cash=initial_cash,
        warmup_days=warmup_days,
        sizer=sizer,
        universe=universe,
    )
    result = engine.run()

    # Calculate metrics
    metrics = calculate_metrics(result)

    # Calculate baselines
    bnh = compute_buy_and_hold(d_dir, syms, start_dt, end_dt, initial_cash)
    random_base = run_random_baseline(result, d_dir, syms, start_dt, end_dt, num_seeds=random_seeds)

    # Generate reports & CSV
    run_output_dir = generate_reports(
        run_id=run_id,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        split=split,
        symbols=syms,
        metrics=metrics,
        result=result,
        bnh=bnh,
        random_base=random_base,
        output_dir=reports_dir,
    )

    # Generate & save chart overlays
    overlays = build_trade_overlays(
        run_id, result.trades, strategy.version, result.rejected_intents
    )
    _save_overlays(run_output_dir / "overlays.json", overlays)

    # Sanitize strategy_params to ensure JSON serializability in registry
    safe_params = {}
    for pk, pv in (strategy_params or {}).items():
        if isinstance(pv, (int, float, str, bool)) or pv is None:
            safe_params[pk] = pv
        elif isinstance(pv, (list, tuple)) and len(pv) <= 50 and all(isinstance(x, (int, float, str, bool)) for x in pv):
            safe_params[pk] = list(pv)
        else:
            safe_params[pk] = f"<{type(pv).__name__} len={len(pv) if hasattr(pv, '__len__') else '?'}>"

    # Register run record
    run_record: dict[str, Any] = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "strategy_name": strategy.name,
        "strategy_version": strategy.version,
        "params": safe_params,

        "agent_versions": {"trend": AGENT_VERSION},
        "symbols": syms,
        "split": split,
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
        "engine_version": BACKTEST_ENGINE_VERSION,
        "git_commit": get_git_commit(),
        "metrics": metrics.to_dict(),
        "random_baseline": random_base.to_dict(),
        "notes": notes,
        "status": "completed",
        "universe_filter": universe_note,
        "universe_skips": result.universe_skips,
        "delisted_exits": result.delisted_exits,
    }

    # Save to local audit registry
    save_run_record_local(run_record, reports_dir / "registry.json")

    # Try saving to PostgreSQL if DB engine is reachable
    try:
        from deskmind.db.session import create_engine

        db_engine = create_engine(settings)
        await save_run_record_db(db_engine, run_record)
        await db_engine.dispose()
    except Exception as e:
        logger.warning("Could not record run in database (using local audit file): %s", e)

    return {
        "run_id": run_id,
        "metrics": metrics,
        "random_baseline": random_base,
        "bnh": bnh,
        "reports_dir": str(run_output_dir),
        "trades_count": len(result.trades),
        "result": result,
    }
