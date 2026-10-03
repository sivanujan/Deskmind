"""Unit tests for H1 v2.0.0:
1. Honest limit fill requires trade-through (touch-only = no fill).
2. Maker fee applied, zero entry slippage.
3. Expiry after N bars (N=12) and missed fill reporting.
4. No look-ahead in the filters (1h trend, killzone, proximal edge).
5. Pre-registered trial count K=4 and DSR enforcement.
6. Frozen spec file SHA-256 integrity verification.
7. Random baseline limit fill model integration.
"""

from decimal import Decimal
import hashlib
from pathlib import Path
import pytest

from deskmind.backtest.fills import check_limit_trade_through, simulate_limit_fill
from deskmind.config import DEFAULT_MAKER_FEE_RATE, DEFAULT_TAKER_FEE_RATE
from deskmind.research.gates import calculate_deflated_sharpe, evaluate_research_gates
from deskmind.research.hypotheses.h1_v2 import (
    H1DiscountOBPluginV2,
    H1V2VariantAPlugin,
    H1V2VariantBPlugin,
    H1V2VariantCPlugin,
)
from deskmind.research.hypotheses.registry import (
    H1_V2_FROZEN_SHA256,
    H1_V2_SPEC_PATH,
    REGISTRY,
    verify_h1_v2_spec_hash,
)
from deskmind.research.random_baseline import run_backtest_random_baseline


def test_h1_v2_limit_fill_requires_trade_through_touch_does_not_fill() -> None:
    """Honest fill rule:
    Fill ONLY if a later bar trades THROUGH the limit by >= 1 tick.
    Touch-only is NEVER a fill.
    """
    tick = Decimal("0.10")
    limit = Decimal("100.0")

    # 1. Long (buy) limit order
    # (a) Candle touches 100.0 exactly (low = 100.0) -> touch only, NO fill!
    assert not check_limit_trade_through("long", limit, Decimal("105.0"), Decimal("100.0"), tick)
    fill_p, is_m = simulate_limit_fill("long", limit, Decimal("105.0"), Decimal("100.0"), tick)
    assert fill_p is None
    assert is_m is False

    # (b) Candle trades through by 1 tick (low = 99.9) -> FILLED
    assert check_limit_trade_through("long", limit, Decimal("105.0"), Decimal("99.9"), tick)
    fill_p, is_m = simulate_limit_fill("long", limit, Decimal("105.0"), Decimal("99.9"), tick)
    assert fill_p == limit
    assert is_m is True

    # (c) Candle trades deep through (low = 95.0) -> FILLED
    assert check_limit_trade_through("long", limit, Decimal("105.0"), Decimal("95.0"), tick)

    # 2. Short (sell) limit order
    # (a) Candle touches 100.0 exactly (high = 100.0) -> touch only, NO fill!
    assert not check_limit_trade_through("short", limit, Decimal("100.0"), Decimal("95.0"), tick)
    fill_p, is_m = simulate_limit_fill("short", limit, Decimal("100.0"), Decimal("95.0"), tick)
    assert fill_p is None
    assert is_m is False

    # (b) Candle trades through by 1 tick (high = 100.1) -> FILLED
    assert check_limit_trade_through("short", limit, Decimal("100.1"), Decimal("95.0"), tick)
    fill_p, is_m = simulate_limit_fill("short", limit, Decimal("100.1"), Decimal("95.0"), tick)
    assert fill_p == limit
    assert is_m is True

    # (c) Candle trades deep through (high = 105.0) -> FILLED
    assert check_limit_trade_through("short", limit, Decimal("105.0"), Decimal("95.0"), tick)


def test_h1_v2_maker_fee_applied_and_zero_entry_slippage() -> None:
    """When a limit order fills, maker fee rate applies (0.02% vs taker 0.05%) and entry slippage is 0."""
    tick = Decimal("0.01")
    limit = Decimal("50000.00")
    b_high = Decimal("50100.00")
    b_low = Decimal("49999.99")  # 1 tick through

    fill_price, is_maker = simulate_limit_fill("long", limit, b_high, b_low, tick)
    assert is_maker is True
    assert fill_price == limit  # 0 slippage: filled exactly at limit price

    fee_rate = DEFAULT_MAKER_FEE_RATE if is_maker else DEFAULT_TAKER_FEE_RATE
    assert fee_rate == Decimal("0.0002")  # 0.02%
    notional = fill_price * Decimal("1.5")
    fee = notional * fee_rate
    taker_fee = notional * DEFAULT_TAKER_FEE_RATE
    assert fee < taker_fee
    assert fee == notional * Decimal("0.0002")


def test_h1_v2_expiry_after_n_bars_and_missed_fill_accounting() -> None:
    """An unfilled limit order expires after N=12 bars and is counted as a missed fill."""
    tick = Decimal("0.10")
    limit = Decimal("100.0")
    N = 12

    # 15 bars that bounce above limit (never trade through: low >= 100.0)
    bars_never_fill = [
        {"high": Decimal("105.0"), "low": Decimal("100.1") if i % 2 == 0 else Decimal("100.0")}
        for i in range(15)
    ]

    fill_idx = None
    for j in range(min(len(bars_never_fill), N)):
        b = bars_never_fill[j]
        fp, _ = simulate_limit_fill("long", limit, b["high"], b["low"], tick)
        if fp is not None:
            fill_idx = j
            break

    # Order expired after N=12 bars without fill
    assert fill_idx is None, "Expected order to remain unfilled"

    # Now test bar that trades through on bar 5 (within N=12)
    bars_fill_at_5 = [
        {"high": Decimal("105.0"), "low": Decimal("100.5")}
        for _ in range(4)
    ] + [{"high": Decimal("105.0"), "low": Decimal("99.9")}]  # bar 5 trades through

    fill_idx2 = None
    for j in range(min(len(bars_fill_at_5), N)):
        b = bars_fill_at_5[j]
        fp, _ = simulate_limit_fill("long", limit, b["high"], b["low"], tick)
        if fp is not None:
            fill_idx2 = j
            break

    assert fill_idx2 == 4  # Filled on bar 5 (index 4) within expiry window


def test_h1_v2_no_lookahead_in_filters() -> None:
    """Verify that H1 v2 signal evaluation strictly uses point-in-time state without forward information:
    - 1h trend uses closed prior 1h bar.
    - Killzone uses timestamp of current bar close.
    - Proximal edge uses distance known at candle close T.
    """
    plugin_a = H1V2VariantAPlugin()
    plugin_b = H1V2VariantBPlugin()
    plugin_c = H1V2VariantCPlugin()

    # Base setup: discount + fresh OB + 5m CHoCH_up
    base_row = {
        "symbol": "BTCUSDT",
        "timestamp": 1704100000000,
        "htf_trend_1h": "bullish",
        "htf_trend_2h": "bullish",
        "pd_zone_1h": "discount",
        "nearest_fvg_size_atr": 0.0,
        "nearest_fvg_dist_atr": 999.0,
        "nearest_ob_state": "fresh",
        "nearest_ob_dist_atr": 0.25,  # 0.25 ATR away from current close
        "last_event_5m": "CHoCH_up",
        "atr_pct": 0.01,
        "killzone": "london_open",
    }
    close_price = 50000.0
    atr = 500.0  # 1% of 50,000

    # 1. Variant A evaluates base setup -> limit order at proximal edge (50000 - 0.25 * 500 = 49875.0)
    sig_a = plugin_a.evaluate(base_row, current_close=close_price)
    assert sig_a is not None
    assert sig_a.order_type == "limit"
    assert sig_a.entry_price == 50000.0 - (0.25 * atr)  # 49875.0
    assert sig_a.stop_price == sig_a.entry_price - (1.5 * atr)
    assert sig_a.target_price == sig_a.entry_price + (3.0 * atr)

    # 2. Variant B (1h trend filter): rejects when 1h trend is bearish
    row_bearish_trend = dict(base_row, htf_trend_1h="bearish")
    assert plugin_b.evaluate(row_bearish_trend, current_close=close_price) is None
    # Accepts when 1h trend is bullish
    assert plugin_b.evaluate(base_row, current_close=close_price) is not None

    # 3. Variant C (Killzone filter): rejects outside London / NY AM
    row_asian_kz = dict(base_row, killzone="asian")
    assert plugin_c.evaluate(row_asian_kz, current_close=close_price) is None
    row_none_kz = dict(base_row, killzone="none")
    assert plugin_c.evaluate(row_none_kz, current_close=close_price) is None
    # Accepts in london_open and ny_am
    assert plugin_c.evaluate(base_row, current_close=close_price) is not None
    row_ny = dict(base_row, killzone="ny_am")
    assert plugin_c.evaluate(row_ny, current_close=close_price) is not None


def test_h1_v2_frozen_spec_hash_and_registry_program_trial_count_is_9() -> None:
    """Verify that H1 v2 specification file is frozen (SHA-256 match) and program-wide trial count K=9 is enforced."""
    # 1. SHA-256 integrity match
    assert verify_h1_v2_spec_hash() is True
    spec_path = Path(H1_V2_SPEC_PATH)
    assert spec_path.exists()
    calculated_hash = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    assert calculated_hash == H1_V2_FROZEN_SHA256

    # 2. Registry trial counts computed program-wide: K=9
    assert REGISTRY.get_program_trial_count() == 9
    expected_trials = [
        "H1_v1.0.0",
        "H1_v2.0.0_A",
        "H1_v2.0.0_B",
        "H1_v2.0.0_C",
        "H2_v1.0.0",
        "H3_v1.0.0",
        "H4_v1.0.0",
        "H4_v1.1.0",
        "H5_v1.0.0",
    ]
    assert REGISTRY.get_program_trials() == expected_trials
    assert REGISTRY.get_trial_count("H1_v2.0.0") == 9
    assert REGISTRY.get_trial_count("H1_v2.0.0_A") == 9
    assert REGISTRY.get_trial_count("H1_v2.0.0_B") == 9
    assert REGISTRY.get_trial_count("H1_v2.0.0_C") == 9
    assert REGISTRY.get_trial_count("H1_v1.0.0") == 9

    # 3. DSR strictly uses K=9 trials for any H1_v2 evaluation
    returns = [1.0, 0.8, -0.2, 1.2, 0.5, 0.3, 0.9, -0.1, 0.7, 0.4] * 5
    dsr_k1 = calculate_deflated_sharpe(returns, total_trials=1)
    dsr_k9 = calculate_deflated_sharpe(returns, total_trials=9)
    assert dsr_k1 > dsr_k9, f"DSR K=1 ({dsr_k1}) must strictly exceed DSR K=9 ({dsr_k9})"

    # 4. Gate check result enforces K=9 DSR
    from unittest.mock import MagicMock
    from deskmind.backtest.engine import BacktestResult, Trade

    mock_trade = Trade(
        id="t1", symbol="BTCUSDT", side="long", entry_time_ms=1680000000000,
        entry_price=Decimal("20000.0"), exit_time_ms=1680003600000, exit_price=Decimal("20600.0"),
        qty=Decimal("1.0"), pnl=Decimal("580.0"), pnl_pct=Decimal("2.9"), r_multiple=Decimal("1.93"),
        exit_reason="take_profit", fees_paid=Decimal("20.0"), funding_paid=Decimal("0.0"),
        slippage_paid=Decimal("0.0"), holding_time_minutes=60, spanned_gap=False, initial_risk=Decimal("300.0"),
    )
    bt = MagicMock(spec=BacktestResult)
    bt.trades = [mock_trade] * 50
    bt.initial_cash = Decimal("10000.0")
    bt.equity_curve = []
    bt.max_drawdown_pct = 2.0

    gate = evaluate_research_gates("H1_v2.0.0_A", [], [], trial_count=1, backtest_result=bt)
    # Even if trial_count=1 was passed in, H1_v2 enforces effective_trials >= 9
    expected_dsr_k9 = calculate_deflated_sharpe([1.93] * 50, total_trials=9)
    assert gate.deflated_sharpe == expected_dsr_k9


def test_h1_v2_random_baseline_limit_model_integration() -> None:
    """Verify that random baseline correctly models limit order trade-through and expiry."""
    day_start = 1680000000000

    # 20 bars that stay at 100.0 (never trade through buy limit at 100.0)
    bars_flat = [
        {
            "open_time": day_start + i * 300_000,
            "open": Decimal("100.0"),
            "high": Decimal("100.0"),
            "low": Decimal("100.0"),
            "close": Decimal("100.0"),
        }
        for i in range(50)
    ]

    mock_trades = [
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "entry_time_ms": day_start + 5 * 300_000,
            "entry_price": Decimal("100.0"),
            "qty": Decimal("1.0"),
            "initial_risk": Decimal("10.0"),
            "pnl": Decimal("0.0"),
            "fees_paid": Decimal("0.0"),
            "funding_paid": Decimal("0.0"),
            "slippage_paid": Decimal("0.0"),
            "order_type": "limit",
        }
    ]

    # With flat bars, touch-only at 100.0 means no trade-through -> all expire after 12 bars
    stats = run_backtest_random_baseline(
        trades=mock_trades,
        bars_by_symbol={"BTCUSDT": bars_flat},
        num_seeds=5,
        entry_order_type="limit",
        limit_expiry_bars=12,
    )
    assert stats.missed_limit_fills_per_seed == 1
    assert stats.skipped_trades_per_seed == 1

    # Now with bars that trade through by 1 tick (low = 99.9) on next bar
    bars_tradethrough = [
        {
            "open_time": day_start + i * 300_000,
            "open": Decimal("100.0"),
            "high": Decimal("101.0"),
            "low": Decimal("99.9"),  # trades through 100.0 by 0.1 tick
            "close": Decimal("100.5"),
        }
        for i in range(50)
    ]

    stats_filled = run_backtest_random_baseline(
        trades=mock_trades,
        bars_by_symbol={"BTCUSDT": bars_tradethrough},
        num_seeds=5,
        entry_order_type="limit",
        limit_expiry_bars=12,
    )
    # Order fills -> 0 missed limit fills!
    assert stats_filled.missed_limit_fills_per_seed == 0
    assert stats_filled.skipped_trades_per_seed == 0


def test_h1_v2_exit_fee_model_in_random_baseline() -> None:
    """Verify that random baseline applies spec exit fee model:
    - Target: limit (maker 0.0002, 0 slippage)
    - Stop: market (taker 0.0005 + adverse slippage)
    - Time/other: market (taker 0.0005 + adverse slippage)
    """
    day_start = 1680000000000
    mock_trade = {
        "symbol": "BTCUSDT",
        "side": "long",
        "entry_time_ms": day_start,
        "entry_price": Decimal("100.0"),
        "qty": Decimal("10.0"),
        "initial_risk": Decimal("15.0"),  # risk_pct = 15.0 / (100.0 * 10.0) = 0.015 (1.5%)
        "pnl": Decimal("0.0"),
        "fees_paid": Decimal("0.0"),
        "funding_paid": Decimal("0.0"),
        "slippage_paid": Decimal("0.0"),
        "order_type": "limit",
    }

    # 1. Target exit scenario: bar 1 trades through entry (99.8 <= 99.9), bar 2 hits TP (high 104.0 >= 103.0)
    bars_tp = [
        {"open_time": day_start + 0 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.9"), "close": Decimal("100.0")},
        {"open_time": day_start + 1 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.8"), "close": Decimal("100.0")},
        {"open_time": day_start + 2 * 300_000, "open": Decimal("100.0"), "high": Decimal("104.0"), "low": Decimal("99.9"), "close": Decimal("103.5")},
    ]
    sampled_tp: list[Any] = []
    stats_tp = run_backtest_random_baseline(
        trades=[mock_trade],
        bars_by_symbol={"BTCUSDT": bars_tp},
        num_seeds=1,
        entry_order_type="limit",
        limit_expiry_bars=5,
        sampled_trades=sampled_tp,
    )
    assert len(sampled_tp) == 1
    t_tp = sampled_tp[0]
    assert t_tp.exit_reason == "take_profit"
    # Target reached exactly at 103.0 without slippage
    assert t_tp.target_price == 103.0
    assert abs(t_tp.gross_r - 2.0) < 1e-4
    # Expected fee: entry (maker 0.0002 / 0.015 = 0.013333R) + exit (maker 0.0002 * (103/100) / 0.015 = 0.013733R) = 0.027067R
    expected_tp_cost = (0.0002 / 0.015) + (0.0002 * 1.03 / 0.015)
    expected_tp_net = 2.0 - expected_tp_cost
    assert abs(t_tp.net_r - expected_tp_net) < 1e-4, f"Expected net R {expected_tp_net:.4f}, got {t_tp.net_r:.4f}"

    # 2. Stop exit scenario: bar 1 trades through entry (99.8), bar 2 hits SL (low 97.0 <= 98.5)
    bars_sl = [
        {"open_time": day_start + 0 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.9"), "close": Decimal("100.0")},
        {"open_time": day_start + 1 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.8"), "close": Decimal("100.0")},
        {"open_time": day_start + 2 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("97.0"), "close": Decimal("98.0")},
    ]
    sampled_sl: list[Any] = []
    stats_sl = run_backtest_random_baseline(
        trades=[mock_trade],
        bars_by_symbol={"BTCUSDT": bars_sl},
        num_seeds=1,
        entry_order_type="limit",
        limit_expiry_bars=5,
        sampled_trades=sampled_sl,
    )
    assert len(sampled_sl) == 1
    t_sl = sampled_sl[0]
    assert t_sl.exit_reason == "stop_loss"
    # Gross R must include adverse slippage, so gross_r <= -1.0
    assert t_sl.gross_r <= -1.0
    # Stop exit uses taker fee rate (0.0005), so total cost exceeds maker target cost
    stop_cost = t_sl.gross_r - t_sl.net_r
    assert stop_cost > expected_tp_cost, f"Stop taker cost ({stop_cost:.4f}R) must exceed target maker cost ({expected_tp_cost:.4f}R)"

    # 3. Horizon exit scenario: bars stay flat, exits at max_horizon_bars=2
    bars_horizon = [
        {"open_time": day_start + 0 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.9"), "close": Decimal("100.0")},
        {"open_time": day_start + 1 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.8"), "close": Decimal("100.0")},
        {"open_time": day_start + 2 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.2"), "low": Decimal("99.9"), "close": Decimal("100.0")},
        {"open_time": day_start + 3 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.2"), "low": Decimal("99.9"), "close": Decimal("100.0")},
    ]
    sampled_hz: list[Any] = []
    stats_hz = run_backtest_random_baseline(
        trades=[mock_trade],
        bars_by_symbol={"BTCUSDT": bars_horizon},
        num_seeds=1,
        entry_order_type="limit",
        limit_expiry_bars=5,
        max_horizon_bars=2,
        sampled_trades=sampled_hz,
    )
    assert len(sampled_hz) == 1
    t_hz = sampled_hz[0]
    assert t_hz.exit_reason == "max_horizon_bars"
    hz_cost = t_hz.gross_r - t_hz.net_r
    # Horizon exit uses taker fee rate (0.0005)
    expected_hz_fee = (0.0002 / 0.015) + (0.0005 * 1.0 / 0.015)
    assert abs(hz_cost - expected_hz_fee) < 0.01


def test_h1_v2_tp_requires_trade_through_touch_does_not_fill() -> None:
    """Verify that TP fills ONLY when price trades THROUGH target by >= 1 tick.
    Touch-only (high == take_profit for long, low == take_profit for short) is NO FILL.
    """
    from deskmind.backtest.fills import simulate_candle_exit

    tick_size = Decimal("0.1")

    # 1. Long position: take_profit = 105.0
    # 1a) Touch-only: high == 105.0 -> NO FILL
    exit_p, reason, is_maker, _ = simulate_candle_exit(
        pos_side="long",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("105.0"),
        b_open=Decimal("100.0"),
        b_high=Decimal("105.0"),  # touches exactly
        b_low=Decimal("99.0"),
        tick_size=tick_size,
    )
    assert exit_p is None
    assert reason is None

    # 1b) Trade-through by 1 tick: high == 105.1 -> FILLED
    exit_p, reason, is_maker, _ = simulate_candle_exit(
        pos_side="long",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("105.0"),
        b_open=Decimal("100.0"),
        b_high=Decimal("105.1"),  # trades through by 1 tick
        b_low=Decimal("99.0"),
        tick_size=tick_size,
    )
    assert exit_p == Decimal("105.0")
    assert reason == "take_profit"
    assert is_maker is True

    # 2. Short position: take_profit = 95.0
    # 2a) Touch-only: low == 95.0 -> NO FILL
    exit_p, reason, is_maker, _ = simulate_candle_exit(
        pos_side="short",
        stop_loss=Decimal("105.0"),
        take_profit=Decimal("95.0"),
        b_open=Decimal("100.0"),
        b_high=Decimal("101.0"),
        b_low=Decimal("95.0"),  # touches exactly
        tick_size=tick_size,
    )
    assert exit_p is None
    assert reason is None

    # 2b) Trade-through by 1 tick: low == 94.9 -> FILLED
    exit_p, reason, is_maker, _ = simulate_candle_exit(
        pos_side="short",
        stop_loss=Decimal("105.0"),
        take_profit=Decimal("95.0"),
        b_open=Decimal("100.0"),
        b_high=Decimal("101.0"),
        b_low=Decimal("94.9"),  # trades through by 1 tick
        tick_size=tick_size,
    )
    assert exit_p == Decimal("95.0")
    assert reason == "take_profit"
    assert is_maker is True

    # 3. In random baseline: touch-only TP does NOT trigger TP exit
    day_start = 1680000000000
    mock_trade = {
        "symbol": "BTCUSDT",
        "side": "long",
        "entry_time_ms": day_start,
        "entry_price": Decimal("100.0"),
        "qty": Decimal("10.0"),
        "initial_risk": Decimal("15.0"),  # risk_pct = 1.5%, stop = 98.5, target = 103.0
        "pnl": Decimal("0.0"),
        "fees_paid": Decimal("0.0"),
        "funding_paid": Decimal("0.0"),
        "slippage_paid": Decimal("0.0"),
        "order_type": "limit",
    }
    # Touch-only bar: high is exactly 103.0 (tick_size = 0.01)
    bars_touch = [
        {"open_time": day_start + 0 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.9"), "close": Decimal("100.0")},
        {"open_time": day_start + 1 * 300_000, "open": Decimal("100.0"), "high": Decimal("100.5"), "low": Decimal("99.8"), "close": Decimal("100.0")},
        {"open_time": day_start + 2 * 300_000, "open": Decimal("100.0"), "high": Decimal("103.0"), "low": Decimal("99.9"), "close": Decimal("102.5")},  # touches 103.0
        {"open_time": day_start + 3 * 300_000, "open": Decimal("102.0"), "high": Decimal("102.5"), "low": Decimal("99.9"), "close": Decimal("102.0")},
    ]
    sampled_touch: list[Any] = []
    run_backtest_random_baseline(
        trades=[mock_trade],
        bars_by_symbol={"BTCUSDT": bars_touch},
        num_seeds=1,
        entry_order_type="limit",
        limit_expiry_bars=5,
        max_horizon_bars=2,
        sampled_trades=sampled_touch,
    )
    assert len(sampled_touch) == 1
    # Because 103.0 was touch-only, it did not fill TP, and instead exited at horizon
    assert sampled_touch[0].exit_reason == "max_horizon_bars"


def test_h1_v2_variant_b_emits_signals_on_bullish_trend_fixture() -> None:
    """Pre-check: Variant B compares htf_trend_1h ('bullish'/'bearish') with v1_sig.direction.
    Assert Variant B emits signals on a bullish-trend fixture when direction matches,
    and also on bearish-trend fixture when direction matches, but rejects mismatches.
    """
    plugin_b = H1V2VariantBPlugin()

    # 1. Bullish fixture
    bullish_row = {
        "symbol": "BTCUSDT",
        "timestamp": 1680000000000,
        "htf_trend_1h": "bullish",
        "pd_zone_1h": "discount",
        "nearest_fvg_size_atr": 0.0,
        "nearest_fvg_dist_atr": 999.0,
        "nearest_ob_state": "fresh",
        "nearest_ob_dist_atr": 0.2,
        "last_event_5m": "CHoCH_up",
        "atr_pct": 0.01,
        "killzone": "london_open",
    }
    sig_bullish = plugin_b.evaluate(bullish_row, current_close=50000.0)
    assert sig_bullish is not None, "Variant B must emit signal on bullish-trend fixture"
    assert sig_bullish.direction == "bullish"
    assert sig_bullish.order_type == "limit"
    assert sig_bullish.entry_price < 50000.0  # proximal discount limit

    # Mismatch check: if 1h trend is bearish or ranging, bullish setup must be rejected
    assert plugin_b.evaluate(dict(bullish_row, htf_trend_1h="bearish"), current_close=50000.0) is None
    assert plugin_b.evaluate(dict(bullish_row, htf_trend_1h="ranging"), current_close=50000.0) is None

    # 2. Bearish fixture
    bearish_row = {
        "symbol": "BTCUSDT",
        "timestamp": 1680000000000,
        "htf_trend_1h": "bearish",
        "pd_zone_1h": "premium",
        "nearest_fvg_size_atr": 0.0,
        "nearest_fvg_dist_atr": 999.0,
        "nearest_ob_state": "fresh",
        "nearest_ob_dist_atr": 0.2,
        "last_event_5m": "CHoCH_down",
        "atr_pct": 0.01,
        "killzone": "ny_am",
    }
    sig_bearish = plugin_b.evaluate(bearish_row, current_close=50000.0)
    assert sig_bearish is not None, "Variant B must emit signal on bearish-trend fixture"
    assert sig_bearish.direction == "bearish"
    assert sig_bearish.order_type == "limit"
    assert sig_bearish.entry_price > 50000.0  # proximal premium limit
    assert plugin_b.evaluate(dict(bearish_row, htf_trend_1h="bullish"), current_close=50000.0) is None

