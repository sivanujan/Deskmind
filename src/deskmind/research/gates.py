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


def calculate_deflated_sharpe(
    returns: Sequence[float],
    total_trials: int = 1,
) -> float:
    """Calculate Deflated Sharpe Ratio (DSR) as defined by Bailey and Lopez de Prado (2014).

    DSR computes the probability that the estimated Sharpe ratio exceeds what could
    be expected by pure chance under multiple testing (selection bias).
    DSR is strictly a probability bounded in [0, 1].

    Documented Inputs:
      - returns: Sequence of periodic or per-trade returns (sample observations).
      - total_trials (K): Total number of strategy trials or variations tested (>= 1).
      - sample_length (N): Number of return observations, len(returns).
      - skewness (gamma_3): Fisher-Pearson standardized sample skewness.
      - kurtosis (gamma_4): Sample kurtosis (Pearson, normal distribution = 3.0).
      - sharpe (SR_hat): Sample Sharpe ratio (mean / std).
      - expected_max_sharpe (SR_star): Expected maximum Sharpe ratio under the null
        hypothesis among K independent trials, using Euler-Mascheroni extreme value
        approximation: SR* = sqrt(2 * ln(K)) + gamma / sqrt(2 * ln(K)) for K > 1.
      - dsr_std_err (sigma_SR): Standard error of the Sharpe ratio adjusted for skew
        and kurtosis: sqrt((1 - gamma_3*SR + ((gamma_4 - 1)/4)*SR^2) / (N - 1)).
      - z: Standardized test statistic: (SR_hat - SR_star) / sigma_SR.
      - DSR: Cumulative normal probability Phi(z) = 0.5 * (1 + erf(z / sqrt(2))).
    """
    n = len(returns)
    if n < 5:
        return 0.0

    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(variance)
    if std_r <= 1e-9:
        return 0.0

    # Sample Sharpe ratio
    sharpe = mean_r / std_r

    # Sample skewness (gamma_3) and kurtosis (gamma_4)
    skew = sum((r - mean_r) ** 3 for r in returns) / (n * (std_r ** 3))
    kurt = sum((r - mean_r) ** 4 for r in returns) / (n * (std_r ** 4))

    # Benchmark SR*: expected maximum Sharpe under null among K trials
    k = max(1, total_trials)
    if k == 1:
        expected_max_sharpe = 0.0
    else:
        # Euler-Mascheroni constant gamma
        gamma_em = 0.5772156649
        two_log_k = 2.0 * math.log(k)
        sqrt_two_log_k = math.sqrt(two_log_k)
        expected_max_sharpe = sqrt_two_log_k + (gamma_em / sqrt_two_log_k)

    # Standard error of Sharpe ratio estimator under non-normality
    var_sr = (1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * (sharpe ** 2)) / (n - 1)
    if var_sr <= 1e-12:
        return 1.0 if sharpe > expected_max_sharpe else 0.0

    sigma_sr = math.sqrt(var_sr)

    # Standardized test statistic
    z = (sharpe - expected_max_sharpe) / sigma_sr

    # DSR is the cumulative normal distribution Phi(z), strictly in [0, 1]
    dsr = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return round(float(max(0.0, min(1.0, dsr))), 4)


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
    trades: list[Any] = []
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

        if hasattr(backtest_result, "trades"):
            trades = list(backtest_result.trades)

    # When real backtest trades are available, evaluate research gates on the trades!
    if backtest_result is not None:
        trades_count = len(trades)
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

        net_r_list: list[float] = []
        gross_r_list: list[float] = []
        sym_r_map: dict[str, list[float]] = {}
        half1_r: list[float] = []
        half2_r: list[float] = []
        # Calendar midpoint 2024-01-01T00:00:00 UTC (1704067200000 ms)
        mid_ms = 1704067200000

        for t in trades:
            init_risk = float(t.initial_risk) if float(t.initial_risk) > 0 else 100.0
            net_r = float(t.r_multiple)
            friction_usd = float(t.fees_paid + t.slippage_paid + t.funding_paid)
            friction_r = friction_usd / init_risk
            gross_r = net_r + friction_r

            net_r_list.append(net_r)
            gross_r_list.append(gross_r)
            sym_r_map.setdefault(t.symbol, []).append(net_r)

            if t.entry_time_ms < mid_ms:
                half1_r.append(net_r)
            else:
                half2_r.append(net_r)

        gross_exp = sum(gross_r_list) / trades_count
        net_exp = sum(net_r_list) / trades_count

        # Both halves must have at least 1 trade and positive net expectancy
        both_halves_pos = (
            len(half1_r) > 0
            and len(half2_r) > 0
            and (sum(half1_r) / len(half1_r) > 0.0)
            and (sum(half2_r) / len(half2_r) > 0.0)
        )

        valid_symbols = {sym: vals for sym, vals in sym_r_map.items() if len(vals) >= 10}
        if valid_symbols:
            pos_symbols = sum(1 for vals in valid_symbols.values() if sum(vals) > 0)
            pos_symbols_pct = (pos_symbols / len(valid_symbols)) * 100.0
        else:
            pos_symbols_pct = 100.0 if net_exp > 0 else 0.0

        dsr = calculate_deflated_sharpe(net_r_list, total_trials=trial_count)

        # Baseline percentile comparison (using gross expectancy vs baseline event study)
        baseline_r = [b.final_r for b in baseline_results] or [0.0]
        baseline_avg = sum(baseline_r) / len(baseline_r)
        baseline_std = math.sqrt(
            sum((b - baseline_avg) ** 2 for b in baseline_r) / max(1, len(baseline_r) - 1)
        ) or 1.0
        z_score = (gross_exp - baseline_avg) / baseline_std
        percentile = min(99.9, max(0.0, (1.0 + math.erf(z_score / math.sqrt(2.0))) / 2.0 * 100.0))

    else:
        # Fallback to EventStudyResult sequence when no backtest result is provided
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

        baseline_r = [b.final_r for b in baseline_results] or [0.0]
        baseline_avg = sum(baseline_r) / len(baseline_r)
        baseline_std = math.sqrt(
            sum((b - baseline_avg) ** 2 for b in baseline_r) / max(1, len(baseline_r) - 1)
        ) or 1.0
        z_score = (gross_exp - baseline_avg) / baseline_std
        percentile = min(99.9, max(0.0, (1.0 + math.erf(z_score / math.sqrt(2.0))) / 2.0 * 100.0))

        by_symbol: dict[str, list[float]] = {}
        for res in results:
            by_symbol.setdefault(res.symbol, []).append(res.final_r - cost_per_trade_r)

        valid_symbols = {sym: vals for sym, vals in by_symbol.items() if len(vals) >= 10}
        if valid_symbols:
            pos_symbols = sum(1 for vals in valid_symbols.values() if sum(vals) > 0)
            pos_symbols_pct = (pos_symbols / len(valid_symbols)) * 100.0
        else:
            pos_symbols_pct = 100.0 if net_exp > 0 else 0.0

        by_half: dict[str, list[float]] = {}
        for res in results:
            by_half.setdefault(res.year_half, []).append(res.final_r - cost_per_trade_r)

        # 0 trades in a half must fail! Both 2023 and 2024 halves must exist and be positive
        has_2023 = any("2023" in k for k in by_half)
        has_2024 = any("2024" in k for k in by_half)
        both_halves_pos = (
            has_2023
            and has_2024
            and len(by_half) >= 2
            and all(sum(vals) > 0 for vals in by_half.values())
        )

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
