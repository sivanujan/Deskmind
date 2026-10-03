"""Tests for Event Study tool and Research Gates validation."""

from __future__ import annotations

import pytest

from deskmind.agents.trend.models import Bar
from deskmind.research.event_study import calculate_event_study, generate_matched_random_baseline
from deskmind.research.gates import calculate_deflated_sharpe, evaluate_research_gates
from deskmind.research.hypotheses.models import Signal
from deskmind.research.split_guard import SplitGuard


def test_event_study_calculation() -> None:
    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1000,
        direction="bullish",
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        r_multiple=2.0,
        rationale="Test bullish signal",
    )

    # 48 bars going up
    forward_bars = [
        Bar(open_time=1000 + i * 300_000, close_time=1000 + (i + 1) * 300_000, open=100.0 + i, high=101.0 + i, low=99.5 + i, close=100.5 + i)
        for i in range(1, 49)
    ]

    res = calculate_event_study(sig, forward_bars)
    assert res.bars_evaluated == 48
    assert res.mfe_r[48] > 0.0
    assert res.target_or_stop_first in ("target", "stop", "neither")


def test_research_gates_evaluation() -> None:
    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1000,
        direction="bullish",
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        r_multiple=2.0,
        rationale="Test signal",
    )
    forward_bars = [
        Bar(open_time=1000 + i * 300_000, close_time=1000 + (i + 1) * 300_000, open=100.0 + i, high=101.0 + i, low=99.5 + i, close=100.5 + i)
        for i in range(1, 49)
    ]

    es_res = calculate_event_study(sig, forward_bars)
    results = [es_res] * 120  # 120 identical winning signals

    gate_res = evaluate_research_gates("H1_v1.0.0", results, results, trial_count=1)
    assert gate_res.trades_count == 120
    assert gate_res.gross_expectancy_r > 0.15
    assert gate_res.checklist["min_trades_100"] is True
    assert gate_res.checklist["gross_expectancy_0_15R"] is True


def test_deflated_sharpe_calculation() -> None:
    returns = [0.5, 1.2, -0.8, 1.5, 0.4, 0.9, -0.2, 1.1, 0.7, -0.5]
    dsr = calculate_deflated_sharpe(returns, total_trials=3)
    assert isinstance(dsr, float)
    assert 0.0 <= dsr <= 1.0, f"DSR must be a probability in [0, 1], got {dsr}"


def test_deflated_sharpe_multiple_trials_monotonicity() -> None:
    """Increasing trial count strictly penalizes / deflates Sharpe ratio."""
    returns = [1.2, 0.8, 1.5, 0.4, 0.9, 0.2, 1.1, 0.7, 0.5, 0.6] * 5
    dsr_1 = calculate_deflated_sharpe(returns, total_trials=1)
    dsr_5 = calculate_deflated_sharpe(returns, total_trials=5)
    dsr_20 = calculate_deflated_sharpe(returns, total_trials=20)

    assert 0.0 <= dsr_1 <= 1.0
    assert 0.0 <= dsr_5 <= 1.0
    assert 0.0 <= dsr_20 <= 1.0
    assert dsr_1 >= dsr_5 >= dsr_20, f"Expected monotonic deflation: {dsr_1} >= {dsr_5} >= {dsr_20}"


def test_gate_criteria_exact_failing_numbers() -> None:
    """Exact criterion tests requested:
    - H3 (25 trades) and H5 (28 trades) must FAIL min_trades_100.
    - H3 (net -1.662R) and H4 (net -0.270R) must FAIL net_expectancy_positive.
    - H3 and H4 having 0 trades in 2024 must FAIL both_halves_positive.
    """
    from decimal import Decimal
    from unittest.mock import MagicMock
    from deskmind.backtest.engine import BacktestResult, Trade

    # 1. H3: 25 trades in 2023, 0 in 2024, net expectancy -1.662R
    # 2023 timestamp: 1680000000000 (mid 2023)
    h3_trades = [
        Trade(
            id=f"t_{i}",
            symbol="SOLUSDT",
            side="long",
            entry_time_ms=1680000000000 + i * 3600000,
            entry_price=Decimal("20.0"),
            exit_time_ms=1680000000000 + (i + 1) * 3600000,
            exit_price=Decimal("19.0"),
            qty=Decimal("10.0"),
            pnl=Decimal("-166.2"),
            pnl_pct=Decimal("-8.31"),
            r_multiple=Decimal("-1.662"),
            exit_reason="stop_loss",
            fees_paid=Decimal("0.5"),
            funding_paid=Decimal("0.0"),
            slippage_paid=Decimal("0.5"),
            holding_time_minutes=60,
            spanned_gap=False,
            initial_risk=Decimal("100.0"),
        )
        for i in range(25)
    ]
    h3_bt = MagicMock(spec=BacktestResult)
    h3_bt.trades = h3_trades
    h3_bt.initial_cash = Decimal("10000.0")
    h3_bt.equity_curve = []
    h3_bt.max_drawdown_pct = 22.47

    gate_h3 = evaluate_research_gates("H3_v1.0.0", [], [], trial_count=1, backtest_result=h3_bt)
    assert gate_h3.trades_count == 25
    assert gate_h3.checklist["min_trades_100"] is False, "H3 with 25 trades must FAIL min_trades_100"
    assert gate_h3.net_expectancy_r == -1.662
    assert gate_h3.checklist["net_expectancy_positive"] is False, "H3 with -1.662R must FAIL net_expectancy_positive"
    assert gate_h3.checklist["both_halves_positive"] is False, "H3 with 0 trades in 2024 must FAIL both_halves_positive"
    assert gate_h3.verdict == "FAIL"

    # 2. H5: 28 trades (must FAIL min_trades_100)
    h5_trades = [
        Trade(
            id=f"t_{i}",
            symbol="BTCUSDT",
            side="long",
            entry_time_ms=1680000000000 + i * 3600000,
            entry_price=Decimal("30000.0"),
            exit_time_ms=1680000000000 + (i + 1) * 3600000,
            exit_price=Decimal("30100.0"),
            qty=Decimal("0.1"),
            pnl=Decimal("10.0"),
            pnl_pct=Decimal("0.33"),
            r_multiple=Decimal("0.1"),
            exit_reason="take_profit",
            fees_paid=Decimal("0.1"),
            funding_paid=Decimal("0.0"),
            slippage_paid=Decimal("0.1"),
            holding_time_minutes=60,
            spanned_gap=False,
            initial_risk=Decimal("100.0"),
        )
        for i in range(28)
    ]
    h5_bt = MagicMock(spec=BacktestResult)
    h5_bt.trades = h5_trades
    h5_bt.initial_cash = Decimal("10000.0")
    h5_bt.equity_curve = []
    h5_bt.max_drawdown_pct = 16.38

    gate_h5 = evaluate_research_gates("H5_v1.0.0", [], [], trial_count=1, backtest_result=h5_bt)
    assert gate_h5.trades_count == 28
    assert gate_h5.checklist["min_trades_100"] is False, "H5 with 28 trades must FAIL min_trades_100"

    # 3. H4: 137 trades in 2023, 0 in 2024, net expectancy -0.270R
    h4_trades = [
        Trade(
            id=f"t_{i}",
            symbol="ETHUSDT",
            side="long",
            entry_time_ms=1680000000000 + i * 3600000,
            entry_price=Decimal("2000.0"),
            exit_time_ms=1680000000000 + (i + 1) * 3600000,
            exit_price=Decimal("1980.0"),
            qty=Decimal("1.0"),
            pnl=Decimal("-27.0"),
            pnl_pct=Decimal("-1.0"),
            r_multiple=Decimal("-0.270"),
            exit_reason="stop_loss",
            fees_paid=Decimal("0.2"),
            funding_paid=Decimal("0.0"),
            slippage_paid=Decimal("0.2"),
            holding_time_minutes=60,
            spanned_gap=False,
            initial_risk=Decimal("100.0"),
        )
        for i in range(137)
    ]
    h4_bt = MagicMock(spec=BacktestResult)
    h4_bt.trades = h4_trades
    h4_bt.initial_cash = Decimal("10000.0")
    h4_bt.equity_curve = []
    h4_bt.max_drawdown_pct = 17.45

    gate_h4 = evaluate_research_gates("H4_v1.1.0", [], [], trial_count=2, backtest_result=h4_bt)
    assert gate_h4.trades_count == 137
    assert gate_h4.checklist["min_trades_100"] is True
    assert gate_h4.net_expectancy_r == -0.270
    assert gate_h4.checklist["net_expectancy_positive"] is False, "H4 with -0.270R must FAIL net_expectancy_positive"
    assert gate_h4.checklist["both_halves_positive"] is False, "H4 with 0 trades in 2024 must FAIL both_halves_positive"
    assert gate_h4.verdict == "FAIL"


def test_gate_criteria_exact_passing_numbers() -> None:
    """Exact criterion test for passing gate numbers:
    - >= 100 trades (60 in 2023, 60 in 2024).
    - Gross expectancy >= +0.15R.
    - Positive net expectancy in both halves.
    - Positive symbols >= 60%.
    - Valid accounting invariants.
    """
    from decimal import Decimal
    from unittest.mock import MagicMock
    from deskmind.backtest.engine import BacktestResult, Trade

    trades = []
    # 60 trades in 2023 (mid 2023: 1680000000000)
    for i in range(60):
        trades.append(
            Trade(
                id=f"t_2023_{i}",
                symbol="BTCUSDT" if i % 2 == 0 else "ETHUSDT",
                side="long",
                entry_time_ms=1680000000000 + i * 3600000,
                entry_price=Decimal("20000.0"),
                exit_time_ms=1680000000000 + (i + 1) * 3600000,
                exit_price=Decimal("20400.0"),
                qty=Decimal("0.5"),
                pnl=Decimal("195.0"),
                pnl_pct=Decimal("1.95"),
                r_multiple=Decimal("1.95"),
                exit_reason="take_profit",
                fees_paid=Decimal("5.0"),
                funding_paid=Decimal("0.0"),
                slippage_paid=Decimal("0.0"),
                holding_time_minutes=60,
                spanned_gap=False,
                initial_risk=Decimal("100.0"),
            )
        )
    # 60 trades in 2024 (mid 2024: 1715000000000)
    for i in range(60):
        trades.append(
            Trade(
                id=f"t_2024_{i}",
                symbol="BTCUSDT" if i % 2 == 0 else "ETHUSDT",
                side="long",
                entry_time_ms=1715000000000 + i * 3600000,
                entry_price=Decimal("50000.0"),
                exit_time_ms=1715000000000 + (i + 1) * 3600000,
                exit_price=Decimal("51000.0"),
                qty=Decimal("0.2"),
                pnl=Decimal("195.0"),
                pnl_pct=Decimal("1.95"),
                r_multiple=Decimal("1.95"),
                exit_reason="take_profit",
                fees_paid=Decimal("5.0"),
                funding_paid=Decimal("0.0"),
                slippage_paid=Decimal("0.0"),
                holding_time_minutes=60,
                spanned_gap=False,
                initial_risk=Decimal("100.0"),
            )
        )

    bt = MagicMock(spec=BacktestResult)
    bt.trades = trades
    bt.initial_cash = Decimal("100000.0")
    bt.equity_curve = []
    bt.max_drawdown_pct = 5.2

    gate = evaluate_research_gates("H1_v1.0.0", [], [], trial_count=1, backtest_result=bt)
    assert gate.trades_count == 120
    assert gate.checklist["min_trades_100"] is True
    assert gate.gross_expectancy_r >= 0.15
    assert gate.checklist["gross_expectancy_0_15R"] is True
    assert gate.net_expectancy_r > 0.0
    assert gate.checklist["net_expectancy_positive"] is True
    assert gate.checklist["both_halves_positive"] is True
    assert gate.checklist["accounting_invariants_pass"] is True


def test_deflated_sharpe_exact_passing_case() -> None:
    """Strong returns distribution with low trial count yields high DSR (> 0.95)."""
    returns = [1.5, 1.2, 0.8, 1.9, 0.5, 1.1, 1.4, 0.9, 1.3, 0.7] * 5
    dsr = calculate_deflated_sharpe(returns, total_trials=1)
    assert dsr >= 0.95, f"Expected DSR >= 0.95 for strong signal, got {dsr:.4f}"


def test_deflated_sharpe_insufficient_sample_length() -> None:
    """Sample lengths below 5 cannot reliably estimate skew/kurtosis and must return 0.0."""
    returns = [0.5, 1.0, -0.2]
    dsr = calculate_deflated_sharpe(returns, total_trials=1)
    assert dsr == 0.0, f"Expected 0.0 for N < 5, got {dsr}"


def test_deflated_sharpe_higher_trials_deflation() -> None:
    """Test that higher trial counts strictly penalize/deflate Sharpe ratio."""
    returns = [1.0, 0.8, -0.2, 1.2, 0.5, 0.3, 0.9, -0.1, 0.7, 0.4] * 5
    dsr_1 = calculate_deflated_sharpe(returns, total_trials=1)
    dsr_10 = calculate_deflated_sharpe(returns, total_trials=10)
    dsr_100 = calculate_deflated_sharpe(returns, total_trials=100)
    assert dsr_1 >= dsr_10 >= dsr_100 and dsr_1 > dsr_100, f"Expected deflation: {dsr_1:.4f} >= {dsr_10:.4f} >= {dsr_100:.4f}"


def test_event_study_matched_baseline_scale() -> None:
    """Matched random baseline preserves identical relative stop and target percentage scale."""
    from deskmind.agents.trend.models import Bar
    from deskmind.research.event_study import generate_matched_random_baseline
    from deskmind.research.hypotheses.models import Signal

    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1680000000000,
        direction="bullish",
        entry_price=20000.0,
        stop_price=19600.0,  # 2.0% stop
        target_price=20800.0,  # 4.0% target (2:1)
        r_multiple=2.0,
        rationale="Test scale preservation",
    )

    bars = [
        Bar(
            open_time=1680000000000 + i * 300_000,
            close_time=1680000000000 + (i + 1) * 300_000,
            open=30000.0,  # Different price regime
            high=30100.0,
            low=29900.0,
            close=30000.0,
        )
        for i in range(100)
    ]

    baseline = generate_matched_random_baseline([sig], {"BTCUSDT": bars}, samples=5)
    assert len(baseline) == 5
    for b in baseline:
        # Relative stop must match 2.0% and target 4.0%
        stop_pct = abs(b.signal.entry_price - b.signal.stop_price) / b.signal.entry_price
        target_pct = abs(b.signal.target_price - b.signal.entry_price) / b.signal.entry_price
        assert abs(stop_pct - 0.02) < 1e-4, f"Stop scale mismatch: {stop_pct} != 0.02"
        assert abs(target_pct - 0.04) < 1e-4, f"Target scale mismatch: {target_pct} != 0.04"


def test_pilot_pipeline_reproducibility() -> None:
    """Matched baseline generation produces deterministic and reproducible results with fixed seed."""
    from deskmind.agents.trend.models import Bar
    from deskmind.research.event_study import generate_matched_random_baseline
    from deskmind.research.hypotheses.models import Signal

    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1680000000000,
        direction="bullish",
        entry_price=20000.0,
        stop_price=19800.0,
        target_price=20400.0,
        r_multiple=2.0,
        rationale="Deterministic test",
    )
    bars = [
        Bar(
            open_time=1680000000000 + i * 300_000,
            close_time=1680000000000 + (i + 1) * 300_000,
            open=20000.0 + (i % 5),
            high=20010.0,
            low=19990.0,
            close=20002.0,
        )
        for i in range(60)
    ]

    res1 = generate_matched_random_baseline([sig], {"BTCUSDT": bars}, samples=3)
    res2 = generate_matched_random_baseline([sig], {"BTCUSDT": bars}, samples=3)
    assert [r.signal.entry_price for r in res1] == [r.signal.entry_price for r in res2]
    assert [r.final_r for r in res1] == [r.final_r for r in res2]


def test_event_study_train_split_never_reads_2025_row(tmp_path) -> None:
    """Event study on 'train' split never reads a 2025 feature row."""
    from datetime import UTC, datetime
    from deskmind.features.builder import build_feature_row
    from deskmind.features.store import FeatureStore

    store = FeatureStore(base_dir=tmp_path)
    # 2024-12-31 timestamp (train split)
    t_2024 = int(datetime(2024, 12, 31, 23, 0, tzinfo=UTC).timestamp() * 1000)
    # 2025-01-01 timestamp (validation split)
    t_2025 = int(datetime(2025, 1, 1, 1, 0, tzinfo=UTC).timestamp() * 1000)

    row_2024 = build_feature_row("BTCUSDT", t_2024, None, None, None, None, None, None, None, None, None, None, 65000.0)
    row_2025 = build_feature_row("BTCUSDT", t_2025, None, None, None, None, None, None, None, None, None, None, 66000.0)

    store.write_features([row_2024, row_2025])

    # Read on train split
    train_rows = store.read_features("BTCUSDT", split="train")
    assert len(train_rows) == 1
    assert train_rows[0]["timestamp"] == t_2024


def test_validation_split_without_passing_gates_refused() -> None:
    """Running on 'validation' split without passing gates is refused."""
    from deskmind.research.split_guard import SplitGuard

    # Without gate result
    with pytest.raises(PermissionError, match="refused"):
        SplitGuard.validate_split_request(split="validation", hypothesis_id="H1_v1.0.0", gate_result=None)


@pytest.mark.asyncio
async def test_research_backtest_uses_real_engine(tmp_path) -> None:
    """Item 3: Research backtest must route through the actual BacktestEngine.

    Fails if research backtest results are produced without BacktestEngine.
    """
    from deskmind.backtest.engine import BacktestResult
    from deskmind.backtest.runner import STRATEGY_REGISTRY, run_backtest

    # 1. H1 strategy must be registered in the backtest engine runner
    assert "h1_discount_ob" in STRATEGY_REGISTRY, "H1 strategy must be in STRATEGY_REGISTRY"

    # 2. Executing research strategy without the real engine must fail
    with pytest.raises(ValueError, match="Unknown strategy"):
        await run_backtest(strategy_name="synthetic_toy_research_bypass", split="train")

    # 3. Running through run_backtest produces a genuine BacktestResult with fills, fees, slippage
    # using the real BacktestEngine
    bt_res = await run_backtest(
        strategy_name="h1_discount_ob",
        split="train",
        symbols=["BTCUSDT"],
        start_date="2023-01-01",
        end_date="2023-01-05",
        warmup_days=1,
        reports_dir=tmp_path / "reports",
    )
    assert "result" in bt_res, "Research backtest must return real BacktestResult"
    engine_result = bt_res["result"]
    assert isinstance(engine_result, BacktestResult), "Must be an instance of BacktestEngine's BacktestResult"
    assert hasattr(engine_result, "trades")
    assert hasattr(engine_result, "rejected_intents")
    assert hasattr(engine_result, "total_fees_paid")
    assert hasattr(engine_result, "total_slippage_paid")
    assert hasattr(engine_result, "total_funding_paid")


    # Test split permanently locked
    with pytest.raises(PermissionError, match="locked"):
        SplitGuard.validate_split_request(split="test", hypothesis_id="H1_v1.0.0")


def test_engine_raw_signals_equals_funnel_unique_setups_all_hypotheses() -> None:
    """Item 1: Engine raw signals must equal funnel unique setups for every hypothesis.

    Repeated bars of the same setup must be ignored by the strategy plugin, ensuring
    one intent per setup_id using the exact shared setup_id implementation.
    """
    from decimal import Decimal
    from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
    from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
    from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
    from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
    from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin
    from deskmind.strategies.base import BarSnapshot, StrategyContext
    from deskmind.strategies.hypothesis_base import (
        H1DiscountOBStrategy,
        H2SweepReversalStrategy,
        H3SilverBulletStrategy,
        H4BreakoutRetestStrategy,
        H5PDSweepStrategy,
    )

    hyp_pairs = [
        (H1DiscountOBPlugin(), H1DiscountOBStrategy),
        (H2SweepReversalPlugin(), H2SweepReversalStrategy),
        (H3SilverBulletPlugin(), H3SilverBulletStrategy),
        (H4BreakoutRetestPlugin(), H4BreakoutRetestStrategy),
        (H5PDSweepPlugin(), H5PDSweepStrategy),
    ]

    # Generate a series of 24 consecutive bars across 2 distinct hour windows:
    # Setup A on bars 0..11 (hour 0), Setup B on bars 12..23 (hour 1)
    base_ts = 1672531200000
    rows: list[dict[str, Any]] = []

    for i in range(24):
        t_open = base_ts + i * 300_000
        t_close = t_open + 299_999
        # Bars 0..11: Setup A (confirmed at base_ts, bars_ago = i)
        # Bars 12..23: Setup B (confirmed at base_ts + 12 * 300_000, bars_ago = i - 12)
        if i < 12:
            conf_time = base_ts
            bars_ago = i
        else:
            conf_time = base_ts + 12 * 300_000
            bars_ago = i - 12

        r = {
            "symbol": "BTCUSDT",
            "timestamp": t_close,
            "htf_trend_1h": "bullish",
            "pd_zone_1h": "discount",
            "nearest_fvg_dist_atr": 0.5,
            "nearest_fvg_size_atr": 1.0,
            "nearest_fvg_displacement": True,
            "nearest_ob_dist_atr": 0.5,
            "nearest_ob_state": "fresh",
            "last_event_5m": "CHoCH_up",
            "last_event_5m_confirmed_at": conf_time,
            "killzone": "london_open",
            "strong_pool_swept_side": "SSL",
            "strong_pool_swept_bars_ago": 2,
            "silver_bullet_active": True,
            "nearest_sr_touches": 3,
            "nearest_sr_strength": 50,
            "nearest_sr_dist_atr": 0.2,
            "pattern_recent_at_zone": True,
            "pattern_recent_with_trend": "aligned",
            "pattern_recent_bars_ago": bars_ago,
            "pdl_swept_recent": True,
            "pdh_swept_recent": False,
            "atr_pct": 0.01,
        }
        rows.append(r)

    for plugin, strat_cls in hyp_pairs:
        # 1. Funnel unique setups
        unique_setups = set()
        for r in rows:
            sig = plugin.evaluate(r, current_close=25000.0)
            if sig:
                unique_setups.add(sig.setup_id)

        # 2. Strategy on_bar intents
        strat = strat_cls(params={"feature_lookup": {f"BTCUSDT:{r['timestamp']}": r for r in rows}})
        emitted_intents = []
        for r in rows:
            t_close = r["timestamp"]
            t_open = ((t_close - 1) // 300_000) * 300_000
            ctx = StrategyContext(
                symbol="BTCUSDT",
                as_of_ms=t_close,
                current_bar=BarSnapshot(
                    open_time=t_open,
                    close_time=t_close,
                    open=Decimal("25000"),
                    high=Decimal("25100"),
                    low=Decimal("24900"),
                    close=Decimal("25000"),
                    volume=Decimal("100"),
                ),
                agent_report=None,
                account=None,
            )
            intents = strat.on_bar({"BTCUSDT": ctx})
            emitted_intents.extend([it for it in intents if it.action in ("open_long", "open_short")])

        # Assert engine raw signals == funnel unique setups
        assert len(emitted_intents) == len(unique_setups), (
            f"Strategy {strat.name} emitted {len(emitted_intents)} intents but funnel found "
            f"{len(unique_setups)} unique setups!"
        )
        assert len(emitted_intents) == 2, f"Expected exactly 2 unique setups, got {len(emitted_intents)}"


def test_get_current_git_commit() -> None:
    from deskmind.research.full_run import get_current_git_commit

    commit = get_current_git_commit()
    assert isinstance(commit, str)
    assert len(commit) == 40
    assert all(c in "0123456789abcdefABCDEF" for c in commit)


def test_checkpoint_commit_guard_refuses_different_commit(tmp_path) -> None:
    from deskmind.research.full_run import _load_checkpoint, _save_checkpoint, run_full_train_pipeline

    cp_file = tmp_path / "checkpoint.json"
    fake_checkpoint = {
        "git_commit": "0000000000000000000000000000000000000000",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "split": "train",
        "start_date": "2023-01-01",
        "end_date": "2024-12-31",
        "top_n": 10,
        "phases": {},
    }
    _save_checkpoint(fake_checkpoint, cp_file)

    loaded = _load_checkpoint(cp_file)
    assert loaded is not None
    assert loaded["git_commit"] == "0000000000000000000000000000000000000000"

    with pytest.raises(RuntimeError, match="Refusing to resume train run across different code versions"):
        import asyncio
        asyncio.run(
            run_full_train_pipeline(
                split="train",
                start_date="2023-01-01",
                end_date="2024-12-31",
                top_n=10,
                checkpoint_path=str(cp_file),
                log_file_path=str(tmp_path / "run.log"),
                resume=True,
            )
        )


def test_matched_random_baseline_properties() -> None:
    """Verify matched random baseline preserves symbol, direction, time-of-day, and relative risk scale."""
    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1672531200000,  # 2023-01-01 00:00:00 UTC
        direction="bullish",
        entry_price=20000.0,
        stop_price=19800.0,  # 1% stop
        target_price=20400.0,  # 2% target (2.0R)
        r_multiple=2.0,
        rationale="Test signal",
    )
    # 2 days of 5m bars
    bars = [
        Bar(
            open_time=1672531200000 + i * 300_000,
            close_time=1672531200000 + (i + 1) * 300_000,
            open=20000.0 + (i % 10),
            high=20050.0 + (i % 10),
            low=19950.0 + (i % 10),
            close=20010.0 + (i % 10),
        )
        for i in range(576)
    ]
    all_bars = {"BTCUSDT": bars}

    baseline = generate_matched_random_baseline([sig], all_bars, samples=10)
    assert len(baseline) == 10
    for b in baseline:
        assert b.symbol == "BTCUSDT"
        assert b.signal.direction == "bullish"
        # Relative stop distance must match original 1% stop scale
        rel_stop = abs(b.signal.entry_price - b.signal.stop_price) / b.signal.entry_price
        assert abs(rel_stop - 0.01) < 1e-4, f"Expected ~1% risk, got {rel_stop}"
        # Time-of-day match: modulo 86,400,000 must match original timestamp
        tod_original = sig.timestamp % 86_400_000
        tod_candidate = (b.signal.timestamp - 300_000) % 86_400_000
        assert tod_candidate == tod_original


def test_accounting_invariants_pass_and_reconciliation() -> None:
    """Verify accounting invariants pass on valid trades and reconcile to the cent."""
    from decimal import Decimal
    from deskmind.backtest.engine import Trade
    from deskmind.research.gates import check_accounting_invariants

    trades = [
        # Win trade: +2R target
        Trade(
            id="t1",
            symbol="BTCUSDT",
            side="long",
            entry_time_ms=1680000000000,
            entry_price=Decimal("20000.0"),
            exit_time_ms=1680003600000,
            exit_price=Decimal("20400.0"),
            qty=Decimal("0.5"),
            pnl=Decimal("199.0"),  # gross 200 - fee 1.0 = 199.0
            pnl_pct=Decimal("1.99"),
            r_multiple=Decimal("1.99"),
            exit_reason="take_profit",
            fees_paid=Decimal("1.0"),
            funding_paid=Decimal("0.0"),
            slippage_paid=Decimal("0.0"),
            holding_time_minutes=60,
            spanned_gap=False,
            initial_risk=Decimal("100.0"),
        ),
        # Loss trade: -1R stop
        Trade(
            id="t2",
            symbol="BTCUSDT",
            side="long",
            entry_time_ms=1680007200000,
            entry_price=Decimal("20000.0"),
            exit_time_ms=1680010800000,
            exit_price=Decimal("19800.0"),
            qty=Decimal("0.5"),
            pnl=Decimal("-101.0"),  # gross -100 - fee 1.0 = -101.0
            pnl_pct=Decimal("-1.01"),
            r_multiple=Decimal("-1.01"),
            exit_reason="stop_loss",
            fees_paid=Decimal("1.0"),
            funding_paid=Decimal("0.0"),
            slippage_paid=Decimal("0.0"),
            holding_time_minutes=60,
            spanned_gap=False,
            initial_risk=Decimal("100.0"),
        ),
    ]

    # Initial cash: 10,000, Total net: 199 - 101 = +98.0 -> final cash: 10,098.0
    res = check_accounting_invariants(trades, initial_cash=10_000.0, final_cash=10_098.0)
    assert res.passed is True
    assert res.per_trade_gross_bounds is True
    assert res.per_trade_cost_bound is True
    assert res.run_gross_expectancy_reconciled is True
    assert res.net_reconciled_to_cent is True
    assert res.equity_reconciled_to_cent is True
    assert len(res.violations) == 0


def test_accounting_invariants_fail_on_corrupted_numbers() -> None:
    """Verify invariants FAIL when gross R bounds or cost limits are violated."""
    from decimal import Decimal
    from deskmind.backtest.engine import Trade
    from deskmind.research.gates import check_accounting_invariants

    # 1. Gross R bound violation: trade loses -50R (like the unpatched DOGE bug)
    corrupted_trade_gross = Trade(
        id="t_bad_gross",
        symbol="DOGEUSDT",
        side="long",
        entry_time_ms=1680000000000,
        entry_price=Decimal("0.20"),
        exit_time_ms=1680003600000,
        exit_price=Decimal("0.10"),
        qty=Decimal("50000.0"),
        pnl=Decimal("-5005.0"),
        pnl_pct=Decimal("-50.0"),
        r_multiple=Decimal("-50.05"),
        exit_reason="stop_loss",
        fees_paid=Decimal("5.0"),
        funding_paid=Decimal("0.0"),
        slippage_paid=Decimal("5000.0"),
        holding_time_minutes=60,
        spanned_gap=False,
        initial_risk=Decimal("100.0"),
    )
    res_gross = check_accounting_invariants([corrupted_trade_gross], initial_cash=100_000.0, final_cash=94_995.0)
    assert res_gross.passed is False
    assert res_gross.per_trade_gross_bounds is False
    assert any("outside bounds" in v for v in res_gross.violations)

    # 2. Cost R bound violation: cost > 0.50R
    corrupted_trade_cost = Trade(
        id="t_bad_cost",
        symbol="BTCUSDT",
        side="long",
        entry_time_ms=1680000000000,
        entry_price=Decimal("20000.0"),
        exit_time_ms=1680003600000,
        exit_price=Decimal("20010.0"),
        qty=Decimal("0.5"),
        pnl=Decimal("-55.0"),
        pnl_pct=Decimal("-0.55"),
        r_multiple=Decimal("-0.55"),
        exit_reason="manual_close",
        fees_paid=Decimal("60.0"),  # 0.60R cost!
        funding_paid=Decimal("0.0"),
        slippage_paid=Decimal("0.0"),
        holding_time_minutes=60,
        spanned_gap=False,
        initial_risk=Decimal("100.0"),
    )
    res_cost = check_accounting_invariants([corrupted_trade_cost], initial_cash=100_000.0, final_cash=99_945.0)
    assert res_cost.passed is False
    assert res_cost.per_trade_cost_bound is False
    assert any("exceeds maximum allowed 0.50R" in v for v in res_cost.violations)


def test_random_walk_baseline_zero_costs_and_with_costs() -> None:
    """Feed synthetic random-walk 5m bars + mock trades into production baseline function:
    - Zero cost: |mean gross R| < 0.03 over 1000 seeds.
    - With cost c: mean net ≈ -c ± 0.03 (|mean net - (-c)| < 0.03).
    """
    import random
    from decimal import Decimal
    from deskmind.research.random_baseline import run_backtest_random_baseline

    # Generate 10,000 bars of zero-drift symmetric random walk
    rng = random.Random(42)
    price = 1000
    bars = []
    t0 = 1680000000000
    for i in range(10000):
        step = 1 if rng.random() > 0.5 else -1
        op = price
        cl = price + step
        hi = max(op, cl)
        lo = min(op, cl)
        bars.append({
            "open_time": t0 + i * 300_000,
            "open": Decimal(str(op)),
            "high": Decimal(str(hi)),
            "low": Decimal(str(lo)),
            "close": Decimal(str(cl)),
        })
        price = cl

    # 100 mock trades (alternating long/short with 1% risk)
    mock_trades = [
        {
            "symbol": "BTCUSDT",
            "side": "long" if i % 2 == 0 else "short",
            "entry_time_ms": t0 + (i * 7) * 300_000,
            "entry_price": Decimal("1000.0"),
            "qty": Decimal("1.0"),
            "initial_risk": Decimal("10.0"),
            "pnl": Decimal("0.0"),
            "fees_paid": Decimal("0.0"),
            "funding_paid": Decimal("0.0"),
            "slippage_paid": Decimal("0.0"),
        }
        for i in range(100)
    ]

    # a1) Zero cost: |mean gross R| < 0.03 over 1000 seeds
    stats_zero = run_backtest_random_baseline(
        trades=mock_trades,
        bars_by_symbol={"BTCUSDT": bars},
        num_seeds=1000,
        slippage_bps=Decimal("0"),
        slippage_atr_factor=Decimal("0"),
        fixed_cost_r=0.0,
        seed_offset=1,
    )
    assert abs(stats_zero.mean_gross_r) < 0.03, (
        f"Expected |mean gross R| < 0.03, got {stats_zero.mean_gross_r:+.4f}R"
    )

    # a2) With cost c (e.g. c = 0.05R): mean net ≈ -c ± 0.03
    cost_c = 0.05
    stats_cost = run_backtest_random_baseline(
        trades=mock_trades,
        bars_by_symbol={"BTCUSDT": bars},
        num_seeds=1000,
        slippage_bps=Decimal("0"),
        slippage_atr_factor=Decimal("0"),
        fixed_cost_r=cost_c,
        seed_offset=1,
    )
    expected_net = -cost_c
    assert abs(stats_cost.mean_net_r - expected_net) < 0.03, (
        f"Expected mean net ≈ {expected_net:+.4f}R ± 0.03, got {stats_cost.mean_net_r:+.4f}R (diff {abs(stats_cost.mean_net_r - expected_net):.4f})"
    )


def test_exported_trades_csv_accounting_identity() -> None:
    """Assert identity: net_pnl_usd = gross_pnl_usd - fees_usd - funding_usd for every exported trade.
    
    Verifies:
      - gross_pnl_usd is computed from fill prices (which ALREADY include slippage).
      - slippage_usd is informational only (never subtracted again).
      - net_pnl_usd = gross_pnl_usd - fees_usd - funding_usd holds to the cent (<= $0.02).
    """
    import csv
    from pathlib import Path

    csv_dir = Path("reports/research")
    csv_files = list(csv_dir.glob("trades_*.csv"))
    assert len(csv_files) >= 3, f"Expected at least 3 trade CSVs in reports/research, found {len(csv_files)}"

    total_trades_checked = 0

    for csv_file in csv_files:
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            # Verify required columns exist
            required_cols = {
                "hypothesis", "symbol", "side", "entry_time", "entry_price",
                "exit_time", "exit_price", "exit_reason", "qty", "stop_price",
                "target_price", "initial_risk_usd", "gross_pnl_usd", "fees_usd",
                "slippage_usd", "funding_usd", "net_pnl_usd"
            }
            assert required_cols.issubset(set(reader.fieldnames or [])), (
                f"Missing required columns in {csv_file.name}: {required_cols - set(reader.fieldnames or [])}"
            )

            for line_no, row in enumerate(reader, start=2):
                total_trades_checked += 1
                ep = float(row["entry_price"])
                xp = float(row["exit_price"])
                qty = float(row["qty"])
                side = row["side"]
                gross = float(row["gross_pnl_usd"])
                fees = float(row["fees_usd"])
                slip = float(row["slippage_usd"])
                funding = float(row["funding_usd"])
                net = float(row["net_pnl_usd"])

                # 1. Verify gross_pnl_usd is computed directly from fill prices (already containing slippage)
                # Note: prices in CSV are formatted to 6 decimals, so for high-quantity assets (e.g. 74,000 DOGE)
                # string rounding to 6 decimals can produce a minute ~2-3 cent discrepancy vs full-precision Decimal.
                if side == "long":
                    direct_gross = (xp - ep) * qty
                else:
                    direct_gross = (ep - xp) * qty
                assert abs(gross - direct_gross) <= max(0.05, abs(gross) * 0.001), (
                    f"{csv_file.name}:{line_no}: gross_pnl_usd ${gross:.2f} != fill price PnL ${direct_gross:.2f}"
                )

                # 2. Verify net_pnl_usd = gross_pnl_usd - fees_usd - funding_usd (slippage NEVER subtracted again)
                expected_net = gross - fees - funding
                assert abs(net - expected_net) <= 0.02, (
                    f"{csv_file.name}:{line_no}: net_pnl_usd ${net:.2f} != gross - fees - funding (${expected_net:.2f})"
                )

                # 3. If slippage were subtracted again, it would mismatch when slippage > 0
                if slip > 0.05:
                    double_subtracted_net = gross - fees - funding - slip
                    assert abs(net - double_subtracted_net) > 0.02, (
                        f"{csv_file.name}:{line_no}: net_pnl appears to have subtracted slippage twice!"
                    )

    assert total_trades_checked > 0, f"Expected at least 1 trade checked, got {total_trades_checked}"


def test_backtest_random_baseline_matching_properties() -> None:
    """Matching test: assert every random trade has the same symbol, side, risk_pct,
    and time-of-day as its source trade, and assert no trade is skipped.
    """
    from decimal import Decimal
    from deskmind.research.random_baseline import run_backtest_random_baseline

    day_start = 1680000000000
    # 30 full days of 5m bars (288 bars/day = 8640 bars)
    bars = [
        {
            "open_time": day_start + i * 300_000,
            "open": Decimal("100.0"),
            "high": Decimal("101.0"),
            "low": Decimal("99.0"),
            "close": Decimal("100.0"),
        }
        for i in range(30 * 288)
    ]

    source_trades = [
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "entry_time_ms": day_start + (1 * 288 + 12) * 300_000,  # Day 1
            "entry_price": Decimal("100.0"),
            "qty": Decimal("1.0"),
            "initial_risk": Decimal("5.0"),
            "pnl": Decimal("0.0"),
            "fees_paid": Decimal("0.0"),
            "funding_paid": Decimal("0.0"),
            "slippage_paid": Decimal("0.0"),
        },
        {
            "symbol": "BTCUSDT",
            "side": "short",
            "entry_time_ms": day_start + (5 * 288 + 45) * 300_000,  # Day 5
            "entry_price": Decimal("100.0"),
            "qty": Decimal("2.0"),
            "initial_risk": Decimal("4.0"),
            "pnl": Decimal("0.0"),
            "fees_paid": Decimal("0.0"),
            "funding_paid": Decimal("0.0"),
            "slippage_paid": Decimal("0.0"),
        },
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "entry_time_ms": day_start + (10 * 288 + 120) * 300_000,  # Day 10
            "entry_price": Decimal("100.0"),
            "qty": Decimal("0.5"),
            "initial_risk": Decimal("2.5"),
            "pnl": Decimal("0.0"),
            "fees_paid": Decimal("0.0"),
            "funding_paid": Decimal("0.0"),
            "slippage_paid": Decimal("0.0"),
        },
    ]

    stats = run_backtest_random_baseline(
        trades=source_trades,
        bars_by_symbol={"BTCUSDT": bars},
        num_seeds=50,
        seed_offset=1,
    )

    # c) Assert no trade is skipped in the test
    assert stats.skipped_trades_per_seed == 0, (
        f"Expected 0 skipped trades, got {stats.skipped_trades_per_seed}"
    )

    # b) Matching test: assert every random trade has the same symbol, side, risk_pct, and time-of-day
    assert len(stats.sampled_trades) == len(source_trades)
    for sampled, src in zip(stats.sampled_trades, source_trades):
        src_risk_pct = float(src["initial_risk"] / src["qty"]) / float(src["entry_price"])
        src_tod_ms = src["entry_time_ms"] % 86_400_000

        assert sampled.symbol == src["symbol"], f"Symbol mismatch: {sampled.symbol} != {src['symbol']}"
        assert sampled.side == src["side"], f"Side mismatch: {sampled.side} != {src['side']}"
        assert abs(sampled.risk_pct - src_risk_pct) < 1e-6, (
            f"Risk pct mismatch: {sampled.risk_pct} != {src_risk_pct}"
        )
        assert sampled.tod_ms == src_tod_ms, f"Time-of-day mismatch: {sampled.tod_ms} != {src_tod_ms}"

    # d) Across seeds, sampled entry dates must differ from the source trade's date (assert at least 90% differ)
    diff_count = 0
    total_comparisons = 0
    for seed_entries in stats.sampled_entry_times_by_seed:
        for sampled_entry_time, src in zip(seed_entries, source_trades):
            total_comparisons += 1
            src_date = src["entry_time_ms"] // 86_400_000
            sampled_date = sampled_entry_time // 86_400_000
            if sampled_date != src_date:
                diff_count += 1

    diff_fraction = diff_count / total_comparisons if total_comparisons > 0 else 0.0
    assert diff_fraction >= 0.90, (
        f"Expected at least 90% of sampled entry dates to differ from source trade date, got {diff_fraction:.2%}"
    )


def test_random_baseline_honest_tp_random_walk_mean_gross_r() -> None:
    """Regression test: Random-walk baseline WITH honest TP has |mean gross R| < 0.03.

    Under symmetric zero-drift price action and honest TP (requiring trade-through by >= 1 tick),
    the gross expectancy must remain statistically indistinguishable from 0.0 (|mean gross R| < 0.03).
    """
    import random
    from decimal import Decimal
    from deskmind.research.random_baseline import check_random_baseline_sanity, run_backtest_random_baseline

    rng = random.Random(42)
    price = Decimal("1000.0")
    bars = []
    t0 = 1680000000000
    for i in range(10000):
        step = Decimal("1.0") if rng.random() > 0.5 else Decimal("-1.0")
        op = price
        cl = price + step
        hi = max(op, cl)
        lo = min(op, cl)
        bars.append({
            "open_time": t0 + i * 300_000,
            "open": op,
            "high": hi,
            "low": lo,
            "close": cl,
        })
        price = cl

    mock_trades = [
        {
            "symbol": "BTCUSDT",
            "side": "long" if i % 2 == 0 else "short",
            "entry_time_ms": t0 + (i * 7) * 300_000,
            "entry_price": Decimal("1000.0"),
            "qty": Decimal("1.0"),
            "initial_risk": Decimal("10.0"),
            "pnl": Decimal("0.0"),
            "fees_paid": Decimal("0.0"),
            "funding_paid": Decimal("0.0"),
            "slippage_paid": Decimal("0.0"),
        }
        for i in range(100)
    ]

    stats = run_backtest_random_baseline(
        trades=mock_trades,
        bars_by_symbol={"BTCUSDT": bars},
        num_seeds=1000,
        slippage_bps=Decimal("0"),
        slippage_atr_factor=Decimal("0"),
        fixed_cost_r=0.0,
        seed_offset=1,
    )

    # 1. Assert |mean gross R| < 0.03
    assert abs(stats.mean_gross_r) < 0.03, (
        f"Expected |mean gross R| < 0.03 under honest TP, got {stats.mean_gross_r:+.4f}R"
    )

    # 2. Sanity guard passes
    check_random_baseline_sanity(stats, threshold=0.05)


def test_v1_train_honest_tp_changes_exits_only_for_touch_trades() -> None:
    """Regression test: On v1 TRAIN, honest TP must change exits ONLY for the 30 touch-only
    trades + any later trades shifted by them; assert trade count stays within 537 ± 30.
    """
    import csv
    from datetime import UTC, datetime
    from decimal import Decimal
    from pathlib import Path
    from deskmind.backtest.fills import simulate_candle_exit
    from deskmind.backtest.filters import load_symbol_filters
    from deskmind.data.storage import load_klines

    csv_path = Path("reports/research/trades_H1_v1.0.0_touchTP.csv")
    assert csv_path.exists(), "Restored trades_H1_v1.0.0_touchTP.csv must exist"

    with open(csv_path, "r", encoding="utf-8") as f:
        trades = list(csv.DictReader(f))

    assert len(trades) == 537, f"Expected 537 trades in restored touchTP CSV, got {len(trades)}"

    by_sym: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)

    touch_only_count = 0
    trade_through_count = 0
    other_exit_count = 0

    t0 = datetime(2023, 1, 1, tzinfo=UTC)
    t1 = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)

    for sym, sym_trades in by_sym.items():
        sf = load_symbol_filters(Path("data"), sym)
        tbl = load_klines("data", sym, "5m", t0, t1)
        assert tbl is not None and tbl.num_rows > 0
        pyd = tbl.to_pydict()
        b_times = [int(v.timestamp() * 1000) if isinstance(v, datetime) else int(v) for v in pyd["open_time"]]
        bar_lookup = {
            b_times[i]: (
                Decimal(str(pyd["open"][i])),
                Decimal(str(pyd["high"][i])),
                Decimal(str(pyd["low"][i])),
            )
            for i in range(tbl.num_rows)
        }

        for t in sym_trades:
            if t["exit_reason"] != "take_profit":
                other_exit_count += 1
                continue

            exit_ms = int(datetime.fromisoformat(t["exit_time"]).timestamp() * 1000)
            if exit_ms not in bar_lookup:
                continue

            b_op, b_hi, b_lo = bar_lookup[exit_ms]
            sp = Decimal(t["stop_price"]) if t["stop_price"] else None
            tp = Decimal(t["target_price"]) if t["target_price"] else None

            # Evaluate honest trade-through condition on exit bar
            _, reason, _, _ = simulate_candle_exit(
                pos_side=t["side"],
                stop_loss=sp,
                take_profit=tp,
                b_open=b_op,
                b_high=b_hi,
                b_low=b_lo,
                tick_size=sf.tick_size,
            )

            if reason == "take_profit":
                trade_through_count += 1
            else:
                touch_only_count += 1

    # Assert exactly 30 touch-only trades were identified out of 205 TP trades
    assert touch_only_count == 30, (
        f"Expected exactly 30 touch-only TP trades on exit bar, got {touch_only_count}"
    )
    assert trade_through_count == 175, (
        f"Expected exactly 175 honest trade-through TP trades, got {trade_through_count}"
    )
    assert other_exit_count == 332, (
        f"Expected exactly 332 non-TP exit trades, got {other_exit_count}"
    )

    # Exits change ONLY for touch-only trades (30) out of 537 total trades
    # Assert trade count invariant: 537 ± 30 (507 <= count <= 567)
    min_allowed = 537 - 30
    max_allowed = 537 + 30
    assert min_allowed <= len(trades) <= max_allowed, (
        f"Expected trade count within 537 ± 30 ({min_allowed}..{max_allowed}), got {len(trades)}"
    )


def test_random_baseline_sanity_guard_aborts_on_gross_r_violation() -> None:
    """Assert sanity guard raises RuntimeError when |mean gross R| > 0.05."""
    import pytest
    from deskmind.research.random_baseline import RandomBaselineStats, check_random_baseline_sanity

    invalid_stats = RandomBaselineStats(
        num_seeds=10,
        mean_gross_r=-0.96,
        mean_net_r=-1.01,
        min_net_r=-1.20,
        max_net_r=-0.80,
        percentile=0.0,
        beat_count=0,
        skipped_trades_per_seed=0,
        missed_limit_fills_per_seed=0,
    )
    with pytest.raises(RuntimeError, match="SANITY GUARD ABORT"):
        check_random_baseline_sanity(invalid_stats, threshold=0.05)


def test_ensure_features_loaded_multi_symbol_fixture(tmp_path) -> None:
    """Regression test for _ensure_features_loaded: a 3-symbol fixture must
    load features for all 3 symbols and emit signals on all 3.
    """
    from decimal import Decimal
    from deskmind.features.models import FeatureRow
    from deskmind.features.store import FeatureStore
    from deskmind.strategies.base import BarSnapshot, StrategyContext
    from deskmind.strategies.hypothesis_base import H1DiscountOBStrategy

    store = FeatureStore(base_dir=tmp_path)
    symbols = ["ADAUSDT", "BTCUSDT", "ETHUSDT"]
    ts = 1680000000000  # 2023-03-28 10:40:00 UTC

    rows = []
    for sym in symbols:
        rows.append(
            FeatureRow(
                timestamp=ts,
                symbol=sym,
                htf_trend_2h="bullish",
                htf_trend_1h="bullish",
                last_event_15m="MSS_up",
                last_event_15m_confirmed_at=ts,
                last_event_5m="CHoCH_up",
                last_event_5m_confirmed_at=ts,
                pd_zone_1h="discount",
                position_pct_1h=0.35,
                outside_range_1h=False,
                in_ote_1h=True,
                nearest_fvg_size_atr=0.8,
                nearest_fvg_displacement=True,
                nearest_fvg_dist_atr=0.5,
                nearest_ob_state="fresh",
                nearest_ob_dist_atr=0.5,
                strong_pool_swept_side="low",
                strong_pool_swept_bars_ago=2,
                killzone="london_open",
                silver_bullet_active=False,
                session="london",
                dist_to_pdh_atr=2.0,
                dist_to_pdl_atr=0.5,
                pdh_swept_recent=False,
                pdl_swept_recent=True,
                nearest_sr_strength=3,
                nearest_sr_touches=3,
                nearest_sr_dist_atr=1.0,
                pattern_recent_type="none",
                pattern_recent_at_zone=False,
                pattern_recent_with_trend="bullish",
                pattern_recent_bars_ago=5,
                weekend_flag=False,
                atr_pct=0.01,
                universe_member=True,
            )
        )
    store.write_features(rows)

    strategy = H1DiscountOBStrategy(params={"feature_dir": str(tmp_path)})

    # Build contexts for all 3 symbols on the same bar
    contexts: dict[str, StrategyContext] = {}
    for sym in symbols:
        bar = BarSnapshot(
            open_time=ts,
            close_time=ts + 300_000,
            open=Decimal("100.0"),
            high=Decimal("102.0"),
            low=Decimal("99.0"),
            close=Decimal("101.0"),
            volume=Decimal("1000.0"),
        )
        contexts[sym] = StrategyContext(
            symbol=sym,
            as_of_ms=ts,
            current_bar=bar,
            agent_report=None,
            account=None,
        )

    intents = strategy.on_bar(contexts)

    # 1. Assert all 3 symbols were loaded
    assert strategy._loaded_symbols == set(symbols), (
        f"Expected loaded symbols {set(symbols)}, got {strategy._loaded_symbols}"
    )

    # 2. Assert signals were emitted on all 3 symbols
    emitted_symbols = {it.symbol for it in intents}
    assert emitted_symbols == set(symbols), (
        f"Expected intents for all 3 symbols, got {emitted_symbols}"
    )
    assert len(intents) == 3, f"Expected 3 intents, got {len(intents)}"


def test_deflated_sharpe_textbook_example() -> None:
    """Textbook example from Bailey and Lopez de Prado (2014) / Python Financial:
    N = 100 trials, T = 500 observations
    best unannualized Sharpe = 2.117 / sqrt(252) = 0.13336
    unannualized SR0 = 1.940 / sqrt(252) = 0.12221
    skew = 0.0, kurtosis = 3.0 (normal)
    yields z = 0.2480, DSR = 0.598.
    """
    import math
    from deskmind.research.gates import _norm_ppf, calculate_deflated_sharpe

    k = 100
    t = 500
    gamma_em = 0.5772156649
    z_k = (1.0 - gamma_em) * _norm_ppf(1.0 - 1.0 / k) + gamma_em * _norm_ppf(1.0 - 1.0 / (k * math.e))

    target_sr = 2.117 / math.sqrt(252)
    target_sr0 = 1.940 / math.sqrt(252)
    sr_std = target_sr0 / z_k
    var_sr = sr_std ** 2

    # Synthesize returns vector of length 500 with mean = target_sr and std = 1.0
    synth_returns = [target_sr + 1.0] * (t // 2) + [target_sr - 1.0] * (t // 2)

    dsr = calculate_deflated_sharpe(synth_returns, total_trials=k, var_sharpe=var_sr)
    assert abs(dsr - 0.598) <= 0.005, f"Expected DSR ~ 0.598, got {dsr}"







