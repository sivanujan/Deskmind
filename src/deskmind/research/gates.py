"""Research Gates evaluation and Deflated Sharpe Ratio calculation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from deskmind.research.event_study import EventStudyResult


@dataclass(frozen=True)
class GateCheckResult:
    hypothesis_id: str
    passed: bool
    trades_count: int  # >= 100 required
    gross_expectancy_r: float  # >= 0.15R required
    net_expectancy_r: float    # > 0 required
    baseline_percentile: float  # >= 95.0 required
    positive_symbols_pct: float  # >= 60.0% required (for symbols with >= 10 trades)
    positive_in_both_halves: bool  # True required
    deflated_sharpe: float
    checklist: dict[str, bool]
    max_drawdown_pct: float = 0.0

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"

    @property
    def random_baseline_pct(self) -> float:
        return self.baseline_percentile

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict
        d["random_baseline_pct"] = self.random_baseline_pct
        return d


def calculate_deflated_sharpe(returns: Sequence[float], total_trials: int = 1) -> float:
    """Calculate Deflated Sharpe Ratio accounting for multiple testing / trial count."""
    n = len(returns)
    if n < 5:
        return 0.0

    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(variance)
    if std_r <= 1e-9:
        return 0.0

    sharpe = (mean_r / std_r) * math.sqrt(252 * 288)  # Annualized 5m bars

    # Skewness and kurtosis adjustment
    skew = sum((r - mean_r) ** 3 for r in returns) / (n * (std_r ** 3))
    kurt = sum((r - mean_r) ** 4 for r in returns) / (n * (std_r ** 4))

    # Haircut for trial count (Euler-Mascheroni approximation of max expected Sharpe)
    gamma = 0.5772156649
    expected_max_sharpe = std_r * ((1 - gamma) * math.sqrt(2 * math.log(max(1, total_trials))) + gamma * math.sqrt(2 * math.log(max(1, total_trials))))
    
    dsr_std = math.sqrt(max(1e-6, (1 - skew * sharpe + ((kurt - 1) / 4) * (sharpe ** 2)) / (n - 1)))
    dsr = (sharpe - expected_max_sharpe) * dsr_std
    return round(float(dsr), 4)


def evaluate_research_gates(
    hypothesis_id: str,
    results: Sequence[EventStudyResult],
    baseline_results: Sequence[EventStudyResult],
    trial_count: int = 1,
    cost_per_trade_r: float = 0.05,
    backtest_result: Any | None = None,
) -> GateCheckResult:
    """Evaluate encoded research gates for TRAIN -> VALIDATION qualification."""
    max_dd_pct = 0.0
    if backtest_result is not None:
        if hasattr(backtest_result, "equity_curve") and backtest_result.equity_curve:
            peak = float(backtest_result.initial_cash)
            for pt in backtest_result.equity_curve:
                eq = float(pt.equity)
                if eq > peak:
                    peak = eq
                elif peak > 0:
                    dd_pct = ((peak - eq) / peak) * 100.0
                    if dd_pct > max_dd_pct:
                        max_dd_pct = dd_pct
        elif hasattr(backtest_result, "max_drawdown_pct"):
            max_dd_pct = float(backtest_result.max_drawdown_pct)

    trades_count = len(results)
    if trades_count == 0:
        return GateCheckResult(
            hypothesis_id=hypothesis_id,
            passed=False,
            trades_count=0,
            gross_expectancy_r=0.0,
            net_expectancy_r=0.0,
            baseline_percentile=0.0,
            positive_symbols_pct=0.0,
            positive_in_both_halves=False,
            deflated_sharpe=0.0,
            checklist={
                "min_trades_100": False,
                "gross_expectancy_0_15R": False,
                "net_expectancy_positive": False,
                "beats_random_95th_pct": False,
                "positive_symbols_60pct": False,
                "both_halves_positive": False,
            },
            max_drawdown_pct=round(max_dd_pct, 2),
        )

    r_multiples = [res.final_r for res in results]
    gross_exp = sum(r_multiples) / trades_count
    net_exp = gross_exp - cost_per_trade_r

    # Baseline percentile comparison
    baseline_r = [b.final_r for b in baseline_results] or [0.0]
    baseline_avg = sum(baseline_r) / len(baseline_r)
    baseline_std = math.sqrt(sum((b - baseline_avg) ** 2 for b in baseline_r) / max(1, len(baseline_r) - 1)) or 1.0
    z_score = (gross_exp - baseline_avg) / baseline_std
    percentile = min(99.9, max(0.0, (1.0 + math.erf(z_score / math.sqrt(2.0))) / 2.0 * 100.0))

    # Symbol breakdown (minimum 10 trades per symbol)
    by_symbol: dict[str, list[float]] = {}
    for res in results:
        by_symbol.setdefault(res.symbol, []).append(res.final_r - cost_per_trade_r)

    valid_symbols = {sym: vals for sym, vals in by_symbol.items() if len(vals) >= 10}
    if valid_symbols:
        pos_symbols = sum(1 for vals in valid_symbols.values() if sum(vals) > 0)
        pos_symbols_pct = (pos_symbols / len(valid_symbols)) * 100.0
    else:
        pos_symbols_pct = 100.0 if net_exp > 0 else 0.0

    # Year halves breakdown
    by_half: dict[str, list[float]] = {}
    for res in results:
        by_half.setdefault(res.year_half, []).append(res.final_r - cost_per_trade_r)

    both_halves_pos = (
        len(by_half) >= 2 and all(sum(vals) > 0 for vals in by_half.values())
    ) or (len(by_half) == 1 and sum(list(by_half.values())[0]) > 0)

    dsr = calculate_deflated_sharpe(r_multiples, total_trials=trial_count)

    checklist = {
        "min_trades_100": trades_count >= 100,
        "gross_expectancy_0_15R": gross_exp >= 0.15,
        "net_expectancy_positive": net_exp > 0.0,
        "beats_random_95th_pct": percentile >= 95.0,
        "positive_symbols_60pct": pos_symbols_pct >= 60.0,
        "both_halves_positive": both_halves_pos,
    }

    all_passed = all(checklist.values())

    return GateCheckResult(
        hypothesis_id=hypothesis_id,
        passed=all_passed,
        trades_count=trades_count,
        gross_expectancy_r=round(gross_exp, 4),
        net_expectancy_r=round(net_exp, 4),
        baseline_percentile=round(percentile, 2),
        positive_symbols_pct=round(pos_symbols_pct, 1),
        positive_in_both_halves=both_halves_pos,
        deflated_sharpe=dsr,
        checklist=checklist,
        max_drawdown_pct=round(max_dd_pct, 2),
    )
