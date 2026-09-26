"""Comprehensive metrics calculations for backtest results.

All calculations follow institutional quant standards.
Money math uses Decimal; ratio/statistical conversions use float for report clarity.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from deskmind.backtest.engine import BacktestResult


@dataclass
class PerformanceMetrics:
    initial_cash: float
    final_equity: float
    net_return_usd: float
    net_return_pct: float
    cagr_pct: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    max_drawdown_duration_days: float
    sharpe_ratio: float
    sortino_ratio: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    avg_r: float
    expectancy_r: float
    expectancy_pct: float
    avg_cost_per_trade_r: float
    gross_expectancy_r: float
    avg_holding_time_minutes: float
    exposure_time_pct: float
    total_fees_paid: float
    total_funding_paid: float
    total_slippage_paid: float
    gross_pnl_usd: float
    total_costs_usd: float
    cost_as_pct_of_gross: float
    liquidations_count: int
    same_bar_sl_tp_count: int
    skipped_signals_count: int
    longest_losing_streak: int
    per_symbol: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_metrics(result: BacktestResult) -> PerformanceMetrics:
    initial_cash_f = float(result.initial_cash)
    final_equity_f = float(result.final_equity)
    net_return_usd = final_equity_f - initial_cash_f
    net_return_pct = (net_return_usd / initial_cash_f) * 100.0 if initial_cash_f > 0 else 0.0

    total_ms = max(1, result.end_time_ms - result.start_time_ms)
    total_days = total_ms / (1000 * 86400)

    # CAGR
    if total_days >= 30 and final_equity_f > 0 and initial_cash_f > 0:
        cagr_pct = ((final_equity_f / initial_cash_f) ** (365.25 / total_days) - 1.0) * 100.0
    else:
        cagr_pct = net_return_pct * (365.25 / max(1.0, total_days))

    # Drawdown analysis
    peak = initial_cash_f
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    max_dd_duration_ms = 0

    cur_dd_start_time = result.start_time_ms

    for pt in result.equity_curve:
        eq = float(pt.equity)
        if eq > peak:
            peak = eq
            cur_dd_start_time = pt.time_ms
        else:
            dd_usd = peak - eq
            dd_pct = (dd_usd / peak) * 100.0 if peak > 0 else 0.0
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
            cur_duration = pt.time_ms - cur_dd_start_time
            if cur_duration > max_dd_duration_ms:
                max_dd_duration_ms = cur_duration

    max_dd_duration_days = max_dd_duration_ms / (1000 * 86400)

    # Daily resampled returns for Sharpe and Sortino
    daily_equities: dict[str, float] = {}
    for pt in result.equity_curve:
        day_str = datetime.fromtimestamp(pt.time_ms / 1000, UTC).strftime("%Y-%m-%d")
        daily_equities[day_str] = float(pt.equity)

    daily_series = list(daily_equities.values())
    if len(daily_series) > 1:
        daily_returns = [
            (daily_series[i] - daily_series[i - 1]) / daily_series[i - 1]
            for i in range(1, len(daily_series))
            if daily_series[i - 1] > 0
        ]
    else:
        daily_returns = []

    if len(daily_returns) > 1:
        mean_ret = sum(daily_returns) / len(daily_returns)
        var = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_ret = math.sqrt(var) if var > 0 else 0.0

        downside_var = sum(min(0.0, r) ** 2 for r in daily_returns) / len(daily_returns)
        downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0

        sharpe_ratio = (mean_ret / std_ret * math.sqrt(365.25)) if std_ret > 0 else 0.0
        sortino_ratio = (mean_ret / downside_std * math.sqrt(365.25)) if downside_std > 0 else 0.0
    else:
        sharpe_ratio = 0.0
        sortino_ratio = 0.0

    # Trade stats
    trades = result.trades
    total_trades = len(trades)
    winning_trades = sum(1 for t in trades if t.pnl > Decimal("0"))
    losing_trades = sum(1 for t in trades if t.pnl <= Decimal("0"))
    win_rate_pct = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_profit = sum((float(t.pnl) for t in trades if t.pnl > Decimal("0")), 0.0)
    gross_loss = abs(sum((float(t.pnl) for t in trades if t.pnl < Decimal("0")), 0.0))

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 999.0 if gross_profit > 0 else 0.0

    r_multiples = [float(t.r_multiple) for t in trades]
    avg_r = (sum(r_multiples) / total_trades) if total_trades > 0 else 0.0

    win_rs = [float(t.r_multiple) for t in trades if t.pnl > Decimal("0")]
    loss_rs = [abs(float(t.r_multiple)) for t in trades if t.pnl <= Decimal("0")]
    avg_win_r = (sum(win_rs) / len(win_rs)) if win_rs else 0.0
    avg_loss_r = (sum(loss_rs) / len(loss_rs)) if loss_rs else 0.0

    p_win = winning_trades / total_trades if total_trades > 0 else 0.0
    p_loss = losing_trades / total_trades if total_trades > 0 else 0.0
    expectancy_r = (p_win * avg_win_r) - (p_loss * avg_loss_r)

    pnl_pcts = [float(t.pnl_pct) for t in trades]
    expectancy_pct = (sum(pnl_pcts) / total_trades) if total_trades > 0 else 0.0

    # Holding times and exposure
    total_holding_min = sum(t.holding_time_minutes for t in trades)
    avg_holding_time = (total_holding_min / total_trades) if total_trades > 0 else 0.0

    total_backtest_min = total_ms / (1000 * 60)
    exposure_time_pct = (
        (total_holding_min / total_backtest_min * 100.0) if total_backtest_min > 0 else 0.0
    )

    # Longest losing streak
    longest_losing_streak = 0
    cur_streak = 0
    for t in trades:
        if t.pnl <= Decimal("0"):
            cur_streak += 1
            if cur_streak > longest_losing_streak:
                longest_losing_streak = cur_streak
        else:
            cur_streak = 0

    # Per-symbol breakdown
    symbols = sorted(set(t.symbol for t in trades))
    per_symbol: dict[str, dict[str, Any]] = {}
    for s in symbols:
        s_trades = [t for t in trades if t.symbol == s]
        s_wins = sum(1 for t in s_trades if t.pnl > Decimal("0"))
        s_pnl = sum((float(t.pnl) for t in s_trades), 0.0)
        s_gp = sum((float(t.pnl) for t in s_trades if t.pnl > Decimal("0")), 0.0)
        s_gl = abs(sum((float(t.pnl) for t in s_trades if t.pnl < Decimal("0")), 0.0))
        s_pf = (s_gp / s_gl) if s_gl > 0 else (999.0 if s_gp > 0 else 0.0)
        s_r = [float(t.r_multiple) for t in s_trades]
        per_symbol[s] = {
            "trades": len(s_trades),
            "win_rate_pct": (s_wins / len(s_trades) * 100.0) if s_trades else 0.0,
            "net_pnl_usd": s_pnl,
            "profit_factor": s_pf,
            "avg_r": (sum(s_r) / len(s_r)) if s_r else 0.0,
            "fees_paid": float(sum((t.fees_paid for t in s_trades), Decimal("0"))),
            "funding_paid": float(sum((t.funding_paid for t in s_trades), Decimal("0"))),
        }

    total_fees_f = float(result.total_fees_paid)
    total_funding_f = float(result.total_funding_paid)
    total_slippage_f = float(result.total_slippage_paid)
    total_costs_usd = total_fees_f + total_funding_f + total_slippage_f
    gross_pnl_usd = net_return_usd + total_costs_usd
    cost_as_pct_of_gross = (
        (total_costs_usd / abs(gross_pnl_usd)) * 100.0 if abs(gross_pnl_usd) > 0 else 0.0
    )

    # Average cost per trade in R and Gross expectancy in R
    cost_r_list: list[float] = []
    gross_r_list: list[float] = []
    for t in result.trades:
        r_init = float(t.initial_risk)
        cost_usd = float(t.fees_paid + t.funding_paid + t.slippage_paid)
        cost_r = (cost_usd / r_init) if r_init > 0 else 0.0
        cost_r_list.append(cost_r)
        gross_r_list.append(float(t.r_multiple) + cost_r)

    avg_cost_per_trade_r = (sum(cost_r_list) / len(cost_r_list)) if cost_r_list else 0.0
    gross_expectancy_r = (sum(gross_r_list) / len(gross_r_list)) if gross_r_list else 0.0

    return PerformanceMetrics(
        initial_cash=initial_cash_f,
        final_equity=final_equity_f,
        net_return_usd=net_return_usd,
        net_return_pct=net_return_pct,
        cagr_pct=cagr_pct,
        max_drawdown_usd=max_dd_usd,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_days=max_dd_duration_days,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        avg_r=avg_r,
        expectancy_r=expectancy_r,
        expectancy_pct=expectancy_pct,
        avg_cost_per_trade_r=avg_cost_per_trade_r,
        gross_expectancy_r=gross_expectancy_r,
        avg_holding_time_minutes=avg_holding_time,
        exposure_time_pct=exposure_time_pct,
        total_fees_paid=float(result.total_fees_paid),
        total_funding_paid=float(result.total_funding_paid),
        total_slippage_paid=float(result.total_slippage_paid),
        gross_pnl_usd=gross_pnl_usd,
        total_costs_usd=total_costs_usd,
        cost_as_pct_of_gross=cost_as_pct_of_gross,
        liquidations_count=result.liquidations_count,
        same_bar_sl_tp_count=result.same_bar_sl_tp_count,
        skipped_signals_count=result.skipped_signals_count,
        longest_losing_streak=longest_losing_streak,
        per_symbol=per_symbol,
    )
