"""Research Gates evaluation and Deflated Sharpe Ratio calculation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from deskmind.research.event_study import EventStudyResult


@dataclass(frozen=True)
class InvariantCheckResult:
    passed: bool
    violations: list[str]
    warnings: list[str]
    per_trade_gross_bounds: bool
    per_trade_cost_bound: bool
    run_gross_expectancy_reconciled: bool
    net_reconciled_to_cent: bool
    equity_reconciled_to_cent: bool
    max_cost_r: float = 0.0
    flagged_cost_count: int = 0
    tolerance: float = 1e-4

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    invariants: InvariantCheckResult | None = None

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


def check_accounting_invariants(
    trades: Sequence[Any],
    initial_cash: float = 100_000.0,
    final_cash: float | None = None,
    max_slippage_buffer_r: float = 0.50,
    tolerance: float = 1e-4,
) -> InvariantCheckResult:
    """Verify core accounting invariants across individual trades and full run.

    Invariants:
      1. Per-trade gross_R within [-(1 + max_slippage_buffer), target_R + buffer], e.g. [-1.50R, +3.50R].
      2. Per-trade cost_R <= 0.50R (flag anything > 0.20R for review).
      3. Per-run: gross expectancy ≈ win_rate * avg_win_R - (1 - win_rate) * avg_loss_R (within tolerance 1e-4).
      4. Net = gross - costs exactly, reconciled to the equity curve to the cent.
    """
    violations: list[str] = []
    warnings: list[str] = []

    if not trades:
        return InvariantCheckResult(
            passed=True,
            violations=[],
            warnings=[],
            per_trade_gross_bounds=True,
            per_trade_cost_bound=True,
            run_gross_expectancy_reconciled=True,
            net_reconciled_to_cent=True,
            equity_reconciled_to_cent=True,
            max_cost_r=0.0,
            flagged_cost_count=0,
            tolerance=tolerance,
        )

    per_trade_gross_ok = True
    per_trade_cost_ok = True
    net_cent_ok = True

    gross_r_list: list[float] = []
    gross_pnl_list: list[float] = []
    costs_list: list[float] = []
    net_pnl_list: list[float] = []
    max_cost_r = 0.0
    flagged_cost_count = 0

    min_gross_r = -(1.0 + max_slippage_buffer_r)
    max_gross_r = 3.50  # 2.0R target + 1.5R buffer

    for t in trades:
        init_risk = float(t.initial_risk) if float(t.initial_risk) > 0 else 100.0
        if t.side == "long":
            gross_pnl = float((t.exit_price - t.entry_price) * t.qty)
        else:
            gross_pnl = float((t.entry_price - t.exit_price) * t.qty)

        gross_r = gross_pnl / init_risk
        cost_usd = float(t.fees_paid + t.funding_paid)
        cost_r = cost_usd / init_risk
        net_pnl = float(t.pnl)
        net_r = net_pnl / init_risk

        gross_r_list.append(gross_r)
        gross_pnl_list.append(gross_pnl)
        costs_list.append(cost_usd)
        net_pnl_list.append(net_pnl)

        if cost_r > max_cost_r:
            max_cost_r = cost_r

        # 1. Per trade gross R bounds: [-(1 + max_slippage_buffer), target_R + buffer]
        if gross_r < min_gross_r or gross_r > max_gross_r:
            per_trade_gross_ok = False
            violations.append(
                f"Trade {t.id} ({t.symbol}): gross_R={gross_r:+.3f} outside bounds [{min_gross_r:+.2f}, +{max_gross_r:.2f}]"
            )

        # 2. Per trade cost R <= 0.5R, flag anything > 0.2R for review
        if cost_r > 0.50:
            per_trade_cost_ok = False
            violations.append(
                f"Trade {t.id} ({t.symbol}): cost_R={cost_r:.3f} exceeds maximum allowed 0.50R limit"
            )
        elif cost_r > 0.20:
            flagged_cost_count += 1
            warnings.append(
                f"Trade {t.id} ({t.symbol}): cost_R={cost_r:.3f} flagged for review (> 0.20R)"
            )

        # 3. Per trade net = gross - costs exactly (to the cent)
        if abs(net_pnl - (gross_pnl - cost_usd)) > 0.01:
            net_cent_ok = False
            violations.append(
                f"Trade {t.id} ({t.symbol}): net_pnl ${net_pnl:.2f} != gross ${gross_pnl:.2f} - costs ${cost_usd:.2f}"
            )

    # 4. Per run: gross expectancy ≈ win_rate * avg_win_R - (1 - win_rate) * avg_loss_R
    win_rs = [r for r in gross_r_list if r > 0.0]
    loss_rs = [r for r in gross_r_list if r <= 0.0]
    wr = len(win_rs) / len(gross_r_list)
    avg_win_r = sum(win_rs) / len(win_rs) if win_rs else 0.0
    avg_loss_r = sum(-r for r in loss_rs) / len(loss_rs) if loss_rs else 0.0
    decomposed_gross_exp = (wr * avg_win_r) - ((1.0 - wr) * avg_loss_r)
    direct_gross_exp = sum(gross_r_list) / len(gross_r_list)
    exp_diff = abs(direct_gross_exp - decomposed_gross_exp)
    run_gross_ok = exp_diff <= tolerance
    if not run_gross_ok:
        violations.append(
            f"Run gross expectancy mismatch: direct={direct_gross_exp:.6f} vs decomposed={decomposed_gross_exp:.6f} (diff {exp_diff:.6e} > {tolerance})"
        )

    # 5. Net = gross - costs exactly, reconciled to the equity curve to the cent
    sum_gross = sum(gross_pnl_list)
    sum_costs = sum(costs_list)
    sum_net = sum(net_pnl_list)
    reconciled_diff = abs(sum_net - (sum_gross - sum_costs))
    if reconciled_diff > 0.01:
        net_cent_ok = False
        violations.append(
            f"Total net PnL ${sum_net:.2f} != total gross ${sum_gross:.2f} - total costs ${sum_costs:.2f} (diff ${reconciled_diff:.4f})"
        )

    equity_cent_ok = True
    if final_cash is not None:
        cash_delta = final_cash - initial_cash
        equity_diff = abs(sum_net - cash_delta)
        if equity_diff > 0.01:
            equity_cent_ok = False
            violations.append(
                f"Equity curve reconciliation failed: total net PnL ${sum_net:.2f} != cash delta ${cash_delta:.2f} (diff ${equity_diff:.4f})"
            )

    passed = (
        per_trade_gross_ok
        and per_trade_cost_ok
        and run_gross_ok
        and net_cent_ok
        and equity_cent_ok
    )

    return InvariantCheckResult(
        passed=passed,
        violations=violations,
        warnings=warnings,
        per_trade_gross_bounds=per_trade_gross_ok,
        per_trade_cost_bound=per_trade_cost_ok,
        run_gross_expectancy_reconciled=run_gross_ok,
        net_reconciled_to_cent=net_cent_ok,
        equity_reconciled_to_cent=equity_cent_ok,
        max_cost_r=round(max_cost_r, 4),
        flagged_cost_count=flagged_cost_count,
        tolerance=tolerance,
    )


def _norm_ppf(p: float) -> float:
    """Acklam rational approximation for standard normal inverse CDF / quantile function."""
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    p_low = 0.02425
    p_high = 1.0 - p_low
    if 0.0 < p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    elif p_low <= p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    elif p_high < p < 1.0:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )
    elif p <= 0.0:
        return -10.0
    else:
        return 10.0


def calculate_deflated_sharpe(
    returns: Sequence[float],
    total_trials: int = 1,
    var_sharpe: float | None = None,
    trial_sharpes: Sequence[float] | None = None,
) -> float:
    """Calculate Deflated Sharpe Ratio (DSR) as defined by Bailey and Lopez de Prado (2014).

    DSR computes the probability that the estimated Sharpe ratio exceeds what could
    be expected by pure chance under multiple testing (selection bias).
    DSR is strictly a probability bounded in [0, 1].

    Documented Inputs:
      - returns: Sequence of periodic or per-trade returns (sample observations).
      - total_trials (K): Total number of strategy trials or variations tested (>= 1).
      - var_sharpe: Variance of Sharpe ratios observed across the K trials (V[{SR_k}]).
      - trial_sharpes: Sequence of Sharpe ratios across all trials (used to compute var_sharpe if omitted).
      - sample_length (N): Number of return observations, len(returns).
      - skewness (gamma_3): Fisher-Pearson standardized sample skewness.
      - kurtosis (gamma_4): Sample kurtosis (Pearson, normal distribution = 3.0).
      - sharpe (SR_hat): Sample Sharpe ratio (mean / std).
      - expected_max_sharpe (SR_0): Expected maximum Sharpe ratio under the null
        hypothesis among K independent trials:
        SR_0 = sqrt(V[{SR_k}]) * ((1 - gamma) * Phi^-1(1 - 1/K) + gamma * Phi^-1(1 - 1/(K*e))).
      - dsr_std_err (sigma_SR): Standard error of the Sharpe ratio adjusted for skew
        and kurtosis: sqrt((1 - gamma_3*SR + ((gamma_4 - 1)/4)*SR^2) / (N - 1)).
      - z: Standardized test statistic: (SR_hat - SR_0) / sigma_SR.
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
        # Standard normal quantile formula from Bailey & Lopez de Prado (2014)
        gamma_em = 0.5772156649
        z_k = (1.0 - gamma_em) * _norm_ppf(1.0 - 1.0 / k) + gamma_em * _norm_ppf(1.0 - 1.0 / (k * math.e))

        if var_sharpe is not None:
            sr_std = math.sqrt(max(0.0, var_sharpe))
        elif trial_sharpes is not None and len(trial_sharpes) > 1:
            m_ts = sum(trial_sharpes) / len(trial_sharpes)
            v_ts = sum((s - m_ts) ** 2 for s in trial_sharpes) / (len(trial_sharpes) - 1)
            sr_std = math.sqrt(v_ts)
        else:
            # Fallback default: unit variance
            sr_std = 1.0

        expected_max_sharpe = sr_std * z_k

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
    trial_count: int | None = None,
    cost_per_trade_r: float = 0.05,
    backtest_result: Any | None = None,
    var_sharpe: float | None = None,
    trial_sharpes: Sequence[float] | None = None,
) -> GateCheckResult:
    """Evaluate encoded research gates for TRAIN -> VALIDATION qualification."""
    from deskmind.research.hypotheses.registry import REGISTRY

    program_k = REGISTRY.get_program_trial_count()
    if trial_count is not None and trial_count > 0:
        effective_trials = max(trial_count, program_k) if hypothesis_id.startswith("H1_v2") else trial_count
    else:
        effective_trials = program_k

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

        invariants_res = check_accounting_invariants(
            trades,
            initial_cash=float(backtest_result.initial_cash) if hasattr(backtest_result, "initial_cash") else 100_000.0,
            final_cash=float(backtest_result.cash) if hasattr(backtest_result, "cash") else None,
        )

        for t in trades:
            init_risk = float(t.initial_risk) if float(t.initial_risk) > 0 else 100.0
            if t.side == "long":
                gross_pnl = float((t.exit_price - t.entry_price) * t.qty)
            else:
                gross_pnl = float((t.entry_price - t.exit_price) * t.qty)

            gross_r = gross_pnl / init_risk
            net_pnl = float(t.pnl)
            net_r = net_pnl / init_risk

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

        dsr = calculate_deflated_sharpe(
            net_r_list,
            total_trials=effective_trials,
            var_sharpe=var_sharpe,
            trial_sharpes=trial_sharpes,
        )

        # Baseline percentile comparison (matched condition: compare strategy signals against matched baseline)
        if results and baseline_results:
            strat_r = [res.final_r for res in results]
            strat_avg = sum(strat_r) / len(strat_r) if strat_r else 0.0
            baseline_r = [b.final_r for b in baseline_results]
            baseline_avg = sum(baseline_r) / len(baseline_r) if baseline_r else 0.0
            baseline_std = math.sqrt(
                sum((b - baseline_avg) ** 2 for b in baseline_r) / max(1, len(baseline_r) - 1)
            ) or 1.0
            z_score = (strat_avg - baseline_avg) / baseline_std
            percentile = min(99.9, max(0.0, (1.0 + math.erf(z_score / math.sqrt(2.0))) / 2.0 * 100.0))
        elif baseline_results:
            baseline_r = [b.final_r for b in baseline_results]
            baseline_avg = sum(baseline_r) / len(baseline_r) if baseline_r else 0.0
            baseline_std = math.sqrt(
                sum((b - baseline_avg) ** 2 for b in baseline_r) / max(1, len(baseline_r) - 1)
            ) or 1.0
            z_score = (gross_exp - baseline_avg) / baseline_std
            percentile = min(99.9, max(0.0, (1.0 + math.erf(z_score / math.sqrt(2.0))) / 2.0 * 100.0))
        else:
            percentile = 0.0

    else:
        # Fallback to EventStudyResult sequence when no backtest result is provided
        invariants_res = None
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
                    "accounting_invariants_pass": False,
                },
                max_drawdown_pct=round(max_dd_pct, 2),
                invariants=None,
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

        dsr = calculate_deflated_sharpe(
            r_multiples,
            total_trials=effective_trials,
            var_sharpe=var_sharpe,
            trial_sharpes=trial_sharpes,
        )

    checklist = {
        "min_trades_100": trades_count >= 100,
        "gross_expectancy_0_15R": gross_exp >= 0.15,
        "net_expectancy_positive": net_exp > 0.0,
        "beats_random_95th_pct": percentile >= 95.0,
        "positive_symbols_60pct": pos_symbols_pct >= 60.0,
        "both_halves_positive": both_halves_pos,
        "accounting_invariants_pass": invariants_res.passed if invariants_res is not None else True,
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
        invariants=invariants_res,
    )
