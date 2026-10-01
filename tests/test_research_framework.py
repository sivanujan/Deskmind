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


