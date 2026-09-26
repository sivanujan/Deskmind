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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from deskmind.agents.trend.config import AGENT_VERSION
from deskmind.backtest.baselines import compute_buy_and_hold, run_random_baseline
from deskmind.backtest.engine import BacktestEngine, Trade
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
from deskmind.strategies.base import BaseStrategy
from deskmind.strategies.trend_follow_v1 import TrendFollowV1

logger = logging.getLogger("deskmind.backtest.runner")

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "trend_follow_v1": TrendFollowV1,
}


def build_trade_overlays(
    run_id: str, trades: list[Trade], strategy_version: str
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
    data_dir: Path | None = None,
    reports_dir: Path = Path("reports/backtests"),
) -> dict[str, Any]:
    settings = get_settings()
    d_dir = data_dir or settings.data_dir
    syms = symbols or list(settings.data_symbols)

    if strategy_name not in STRATEGY_REGISTRY:
        raise ValueError(
            f"Unknown strategy {strategy_name!r}. Available: {list(STRATEGY_REGISTRY.keys())}"
        )

    # Determine date range
    if start_date and end_date:
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(end_date).replace(tzinfo=UTC)
    else:
        if split not in BACKTEST_SPLITS:
            raise ValueError(f"Unknown split {split!r}. Available: {list(BACKTEST_SPLITS.keys())}")
        s_str, e_str = BACKTEST_SPLITS[split]
        start_dt = datetime.fromisoformat(s_str).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(e_str).replace(tzinfo=UTC)

    # Validate split access (Locked test set enforcement)
    validate_split_access(split, start_dt, end_dt, unlock_test, confirm_test_unlock)

    # Initialize strategy
    strat_cls = STRATEGY_REGISTRY[strategy_name]
    strategy = strat_cls(strategy_params or {})

    # Run ID
    timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_id = f"{strategy.name}_{split}_{timestamp_str}"

    # Execute backtest engine
    engine = BacktestEngine(
        strategy=strategy,
        symbols=syms,
        data_dir=d_dir,
        start=start_dt,
        end=end_dt,
        initial_cash=initial_cash,
    )
    result = engine.run()

    # Calculate metrics
    metrics = calculate_metrics(result)

    # Calculate baselines
    bnh = compute_buy_and_hold(d_dir, syms, start_dt, end_dt, initial_cash)
    random_base = run_random_baseline(result, d_dir, syms, start_dt, end_dt)

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
    overlays = build_trade_overlays(run_id, result.trades, strategy.version)
    _save_overlays(run_output_dir / "overlays.json", overlays)

    # Register run record
    run_record: dict[str, Any] = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "strategy_name": strategy.name,
        "strategy_version": strategy.version,
        "params": strategy_params or {},
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
    }
