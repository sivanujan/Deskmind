"""Tests for the deterministic Risk Manager and Circuit Breakers."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deskmind.backtest.filters import SymbolFilters
from deskmind.risk.breakers import CircuitBreakerTracker
from deskmind.risk.config import (
    RiskConfig,
)
from deskmind.risk.evaluator import evaluate
from deskmind.risk.models import (
    AccountState,
    IntentPayload,
    MarketState,
    ReasonCode,
)


def make_valid_btc_filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000.0"),
        min_notional=Decimal("5.0"),
        price_precision=2,
        quantity_precision=3,
    )


def make_valid_intent(
    symbol: str = "BTCUSDT",
    side: str = "long",
    entry: Decimal = Decimal("60000.0"),
    stop: Decimal = Decimal("58000.0"),
    target: Decimal = Decimal("64000.0"),
) -> IntentPayload:
    return IntentPayload(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        expected_entry=entry,
        stop_loss=stop,
        take_profit=target,
        strategy_name="test_strat",
        strategy_version="1.0",
        entry_timeframe="15m",
    )


def make_valid_account(
    equity: Decimal = Decimal("10000.0"),
    available: Decimal = Decimal("10000.0"),
    open_positions: list[str] | None = None,
    halt_state: str = "normal",
) -> AccountState:
    return AccountState(
        equity=equity,
        available_balance=available,
        open_positions=open_positions or [],
        today_realized_pnl=Decimal("0"),
        peak_equity=equity,
        consecutive_losses=0,
        halt_state=halt_state,
        start_of_day_equity=equity,
    )


def make_valid_market(
    mark: Decimal = Decimal("60000.0"),
    atr_15m: Decimal = Decimal("600.0"),
    funding_rate: Decimal = Decimal("0.0001"),
    next_funding_ms: int = 7200000,
    filters: SymbolFilters | None = None,
) -> MarketState:
    return MarketState(
        mark_price=mark,
        atr_by_tf={"15m": atr_15m, "5m": atr_15m, "1h": atr_15m},
        next_funding_time_ms=next_funding_ms,
        current_funding_rate=funding_rate,
        filters=filters or make_valid_btc_filters(),
        is_live=False,
    )


# ------------------------------------------------------------------------------
# 1. Sanity Checks & Graceful Rejections
# ------------------------------------------------------------------------------


def test_sanity_zero_or_negative_equity():
    intent = make_valid_intent()
    account = make_valid_account(equity=Decimal("0"))
    market = make_valid_market()
    config = RiskConfig()

    dec = evaluate(intent, account, market, config)
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.SANITY_EQUITY_POSITIVE and not r.passed for r in dec.reasons)

    account_neg = make_valid_account(equity=Decimal("-500.0"))
    dec_neg = evaluate(intent, account_neg, market, config)
    assert dec_neg.status == "rejected"


def test_sanity_stop_on_wrong_side():
    # Long with stop above entry
    intent_long_bad = make_valid_intent(side="long", entry=Decimal("60000"), stop=Decimal("61000"))
    dec = evaluate(intent_long_bad, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.SANITY_STOP_SIDE and not r.passed for r in dec.reasons)

    # Short with stop below entry
    intent_short_bad = make_valid_intent(
        side="short", entry=Decimal("60000"), stop=Decimal("59000")
    )
    dec_s = evaluate(intent_short_bad, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec_s.status == "rejected"
    assert any(r.code == ReasonCode.SANITY_STOP_SIDE and not r.passed for r in dec_s.reasons)


def test_sanity_zero_stop_distance():
    intent = make_valid_intent(entry=Decimal("60000"), stop=Decimal("60000"))
    dec = evaluate(intent, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "rejected"


# ------------------------------------------------------------------------------
# 2. Stop Distance Bounds & Friction Checks (Rule 2)
# ------------------------------------------------------------------------------


def test_stop_too_tight_rejected():
    # Min stop = max(0.5 * ATR, 0.3% price). With ATR=600, 0.5*ATR = 300. 0.3% of 60k = 180.
    # Stop dist = 200 -> below 300 min stop
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("59800"), target=Decimal("65000")
    )
    dec = evaluate(
        intent, make_valid_account(), make_valid_market(atr_15m=Decimal("600")), RiskConfig()
    )
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.STOP_TOO_TIGHT and not r.passed for r in dec.reasons)


def test_stop_too_wide_rejected():
    # Max stop = 8% of price = 4800 on 60,000. Stop dist = 5500 -> exceeds 8%
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("54500"), target=Decimal("75000")
    )
    dec = evaluate(intent, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.STOP_TOO_WIDE and not r.passed for r in dec.reasons)


def test_friction_exceeds_15_percent_of_risk():
    # If stop is narrow enough that round-trip friction > 15% of risk
    # Taker fee = 0.05% x 2 = 0.10%. Slippage ~ 0.10% x 2 = 0.20%. Total cost ~ 0.30% of price ($180).
    # If stop dist is $500, cost / risk = 180 / 500 = 36% > 15%
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("59500"), target=Decimal("65000")
    )
    dec = evaluate(
        intent, make_valid_account(), make_valid_market(atr_15m=Decimal("400")), RiskConfig()
    )
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.COSTS_EXCEED_RISK and not r.passed for r in dec.reasons)


# ------------------------------------------------------------------------------
# 3. Net Reward:Risk (Rule 3)
# ------------------------------------------------------------------------------


def test_net_reward_risk_below_1_point_5_rejected():
    # 1:1 gross reward to risk will fail net reward:risk >= 1.5
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("62000")
    )
    dec = evaluate(intent, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.REWARD_RISK_RATIO and not r.passed for r in dec.reasons)


def test_net_reward_risk_above_1_point_5_approved():
    # 2:1 gross reward to risk passes easily
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("64000")
    )
    dec = evaluate(intent, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "approved"
    assert any(r.code == ReasonCode.REWARD_RISK_RATIO and r.passed for r in dec.reasons)


# ------------------------------------------------------------------------------
# 4. Position Sizing & Invariants (Rule 4)
# ------------------------------------------------------------------------------


def test_actual_risk_after_rounding_never_exceeds_allowed():
    # allowed risk = 1% of 10000 = $100.
    # Stop distance = 1777. Raw qty = 100 / 1777 = 0.05627. Step size = 0.001.
    # Qty rounded = 0.056. Actual risk = 0.056 * 1777 = $99.512 <= $100.00
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58223"), target=Decimal("65000")
    )
    dec = evaluate(intent, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "approved"
    assert dec.actual_risk_usd <= dec.allowed_risk_usd


def test_min_qty_filter_rejection():
    # Small equity ($100), risk = $1. Stop distance = $2000. Raw qty = 0.0005 BTC < minQty 0.001
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("64000")
    )
    account = make_valid_account(equity=Decimal("100.0"), available=Decimal("100.0"))
    dec = evaluate(intent, account, make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(
        (r.code == ReasonCode.EXCHANGE_FILTER_MIN_QTY or r.code == ReasonCode.POSITION_SIZE_ZERO)
        and not r.passed
        for r in dec.reasons
    )


# ------------------------------------------------------------------------------
# 5. Leverage Selection & Liquidation Buffer (Rule 5)
# ------------------------------------------------------------------------------


def test_leverage_lowest_integer_and_buffer_check():
    # Equity = $10,000, Notional = $6,000. Balance = $10,000.
    # Lowest integer leverage to fit in balance is 1x.
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("64000")
    )
    dec = evaluate(intent, make_valid_account(), make_valid_market(), RiskConfig())
    assert dec.status == "approved"
    assert dec.leverage == 1
    # Long liq price for 1x isolated margin is ~0, strictly below stop $58,000
    assert dec.liquidation_price < dec.stop_distance_usd or dec.liquidation_price < Decimal("58000")


def test_leverage_reduced_when_balance_limited():
    # If available balance is only $1,000, and notional is $3,000, leverage must be at least 3x
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("64000")
    )
    account = make_valid_account(equity=Decimal("10000"), available=Decimal("1000"))
    dec = evaluate(intent, account, make_valid_market(), RiskConfig())
    assert dec.status == "approved"
    assert dec.leverage == 3
    assert dec.isolated_margin <= account.available_balance


# ------------------------------------------------------------------------------
# 6. Notional Cap (Rule 6)
# ------------------------------------------------------------------------------


def test_notional_cap_rejection():
    # Cap is 3x equity = $30,000 for $10k equity.
    # If notional exceeds $30k, reject.
    # Stop distance very tight (e.g. $100 on $10k price) giving huge notional
    intent = make_valid_intent(
        entry=Decimal("10000"), stop=Decimal("9960"), target=Decimal("11000")
    )
    _ = evaluate(
        intent,
        make_valid_account(equity=Decimal("1000")),
        make_valid_market(mark=Decimal("10000"), atr_15m=Decimal("20")),
        RiskConfig(),
    )
    # Stop distance = 40. Allowed risk = 10. Qty = 0.25 BTC. Notional = 2500 (2.5x equity, ok).
    # If stop distance = 10 -> Qty = 1.0 BTC. Notional = 10,000 (10x equity, exceeds 3x cap)
    intent_tight = make_valid_intent(
        entry=Decimal("10000"), stop=Decimal("9990"), target=Decimal("11000")
    )
    dec_tight = evaluate(
        intent_tight,
        make_valid_account(equity=Decimal("1000")),
        make_valid_market(mark=Decimal("10000"), atr_15m=Decimal("5")),
        RiskConfig(),
    )
    assert dec_tight.status == "rejected"
    assert any(r.code == ReasonCode.NOTIONAL_CAP and not r.passed for r in dec_tight.reasons)


# ------------------------------------------------------------------------------
# 7. Portfolio Limits (Rule 7)
# ------------------------------------------------------------------------------


def test_portfolio_max_one_position_phase_1():
    intent = make_valid_intent()
    account_with_pos = make_valid_account(open_positions=["ETHUSDT"])
    dec = evaluate(intent, account_with_pos, make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.PORTFOLIO_MAX_POSITIONS and not r.passed for r in dec.reasons)


# ------------------------------------------------------------------------------
# 8. Circuit Breakers (Rule 8)
# ------------------------------------------------------------------------------


def test_circuit_breaker_drawdown_halving():
    # 10% drawdown from peak ($10,000 -> $9,000) halves per-trade risk (1.0% -> 0.5%)
    intent = make_valid_intent(
        entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("64000")
    )
    account = AccountState(
        equity=Decimal("8900.0"),  # 11% drawdown
        available_balance=Decimal("8900.0"),
        open_positions=[],
        today_realized_pnl=Decimal("0"),
        peak_equity=Decimal("10000.0"),
        consecutive_losses=0,
        halt_state="normal",
        start_of_day_equity=Decimal("8900.0"),
    )
    dec = evaluate(intent, account, make_valid_market(), RiskConfig())
    # Allowed risk should be halved: 0.5% of 8900 = $44.50
    assert dec.allowed_risk_usd == Decimal("44.50")


def test_circuit_breaker_drawdown_15_pct_halt():
    # 15% drawdown halts trading
    intent = make_valid_intent()
    account = AccountState(
        equity=Decimal("8400.0"),  # 16% drawdown
        available_balance=Decimal("8400.0"),
        open_positions=[],
        today_realized_pnl=Decimal("0"),
        peak_equity=Decimal("10000.0"),
        consecutive_losses=0,
        halt_state="drawdown_halt",
        start_of_day_equity=Decimal("8400.0"),
    )
    dec = evaluate(intent, account, make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(
        r.code == ReasonCode.CIRCUIT_BREAKER_DRAWDOWN_HALT and not r.passed for r in dec.reasons
    )


def test_circuit_breaker_drawdown_20_pct_full_stop_and_reset():
    tracker = CircuitBreakerTracker(initial_equity=Decimal("10000"))
    # Equity drops to 7,900 (21% drawdown)
    tracker.on_time_and_equity(timestamp_ms=100000, current_equity=Decimal("7900"))
    assert tracker.halt_state == "full_stop"

    account = tracker.get_account_state(equity=Decimal("7900"), available_balance=Decimal("7900"))
    dec = evaluate(make_valid_intent(), account, make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.CIRCUIT_BREAKER_FULL_STOP and not r.passed for r in dec.reasons)

    # Manual reset
    success = tracker.reset_full_stop(current_equity=Decimal("7900"), timestamp_ms=105000)
    assert success is True
    assert tracker.halt_state == "normal"
    assert tracker.peak_equity == Decimal("7900")


def test_circuit_breaker_daily_loss_3_pct_and_utc_rollover():
    tracker = CircuitBreakerTracker(initial_equity=Decimal("10000"))
    t0 = 1700000000000  # Day 1
    tracker.on_time_and_equity(timestamp_ms=t0, current_equity=Decimal("10000"))

    # Lose $350 (3.5% of start of day $10k)
    tracker.record_trade_exit(pnl=Decimal("-350.0"), exit_time_ms=t0 + 1000)
    assert tracker.halt_state == "daily_loss_halt"

    # Evaluator must reject
    account = tracker.get_account_state(equity=Decimal("9650"), available_balance=Decimal("9650"))
    dec = evaluate(make_valid_intent(), account, make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(
        r.code == ReasonCode.CIRCUIT_BREAKER_DAILY_LOSS and not r.passed for r in dec.reasons
    )

    # Next UTC day arrives (+24h)
    t_next_day = t0 + (86400 * 1000)
    tracker.on_time_and_equity(timestamp_ms=t_next_day, current_equity=Decimal("9650"))
    assert tracker.halt_state == "normal"
    assert tracker.today_realized_pnl == Decimal("0")


def test_circuit_breaker_consecutive_losses_and_cooldown():
    tracker = CircuitBreakerTracker(initial_equity=Decimal("10000"))
    t0 = 1700000000000
    for i in range(4):
        tracker.record_trade_exit(pnl=Decimal("-20.0"), exit_time_ms=t0 + i * 1000)

    assert tracker.consecutive_losses == 4
    assert tracker.halt_state == "streak_cooldown"

    account = tracker.get_account_state(equity=Decimal("9920"), available_balance=Decimal("9920"))
    dec = evaluate(make_valid_intent(), account, make_valid_market(), RiskConfig())
    assert dec.status == "rejected"
    assert any(
        r.code == ReasonCode.CIRCUIT_BREAKER_STREAK_COOLDOWN and not r.passed for r in dec.reasons
    )

    # 24 hours later
    t_after_24h = t0 + (25 * 3600 * 1000)
    tracker.on_time_and_equity(timestamp_ms=t_after_24h, current_equity=Decimal("9920"))
    assert tracker.halt_state == "normal"
    assert tracker.consecutive_losses == 0


# ------------------------------------------------------------------------------
# 9. Funding Guard (Rule 9)
# ------------------------------------------------------------------------------


def test_funding_guard_adverse_rate_within_30_min_rejected():
    # Long position, next funding in 15 minutes, rate is +0.06% (above 0.05% threshold)
    intent = make_valid_intent(side="long")
    market = make_valid_market(funding_rate=Decimal("0.0006"), next_funding_ms=15 * 60 * 1000)
    dec = evaluate(intent, make_valid_account(), market, RiskConfig())
    assert dec.status == "rejected"
    assert any(r.code == ReasonCode.FUNDING_GUARD and not r.passed for r in dec.reasons)


def test_funding_guard_favorable_rate_approved():
    # Long position, next funding in 15 minutes, but funding rate is NEGATIVE (-0.06%)
    # Long receives funding -> favorable -> approved!
    intent = make_valid_intent(
        side="long", entry=Decimal("60000"), stop=Decimal("58000"), target=Decimal("64000")
    )
    market = make_valid_market(funding_rate=Decimal("-0.0006"), next_funding_ms=15 * 60 * 1000)
    dec = evaluate(intent, make_valid_account(), market, RiskConfig())
    assert dec.status == "approved"


# ------------------------------------------------------------------------------
# 10. Hard Safety Caps Enforcement
# ------------------------------------------------------------------------------


def test_hard_safety_caps_cannot_be_exceeded():
    with pytest.raises(ValueError, match="exceeds hard cap"):
        RiskConfig(risk_pct=Decimal("0.05"))  # Hard cap is 0.02

    with pytest.raises(ValueError, match="exceeds hard cap"):
        RiskConfig(max_stop_price_pct=Decimal("0.20"))  # Hard cap is 0.15

    with pytest.raises(ValueError, match="exceeds hard cap"):
        RiskConfig(max_leverage=15)  # Hard cap is 10

    with pytest.raises(ValueError, match="exceeds hard cap"):
        RiskConfig(max_notional_mult=Decimal("6.0"))  # Hard cap is 5.0


# ------------------------------------------------------------------------------
# 11. Determinism
# ------------------------------------------------------------------------------


def test_evaluator_is_strictly_deterministic():
    intent = make_valid_intent()
    account = make_valid_account()
    market = make_valid_market()
    config = RiskConfig()

    first_decision = evaluate(intent, account, market, config)
    for _ in range(50):
        next_decision = evaluate(intent, account, market, config)
        assert first_decision.to_dict() == next_decision.to_dict()


# ------------------------------------------------------------------------------
# 12. Property-Based Testing (Hypothesis)
# ------------------------------------------------------------------------------


@given(
    equity=st.floats(min_value=-1000, max_value=500000, allow_nan=False),
    entry=st.floats(min_value=0.01, max_value=150000, allow_nan=False),
    stop_mult=st.floats(min_value=0.01, max_value=2.0, allow_nan=False),
    target_mult=st.floats(min_value=0.5, max_value=3.0, allow_nan=False),
    avail_mult=st.floats(min_value=0.0, max_value=1.5, allow_nan=False),
)
@settings(max_examples=100, deadline=None)
def test_hypothesis_evaluator_invariants(
    equity: float,
    entry: float,
    stop_mult: float,
    target_mult: float,
    avail_mult: float,
):
    """Never crashes with unhandled exception; approved decisions satisfy invariants."""
    equity_d = Decimal(f"{equity:.2f}")
    entry_d = Decimal(f"{entry:.4f}")
    stop_d = Decimal(f"{entry * stop_mult:.4f}")
    target_d = Decimal(f"{entry * target_mult:.4f}")
    avail_d = Decimal(f"{max(0.0, equity * avail_mult):.2f}")

    intent = IntentPayload(
        symbol="BTCUSDT",
        side="long",
        expected_entry=entry_d,
        stop_loss=stop_d,
        take_profit=target_d,
        strategy_name="hypo_test",
        strategy_version="1.0",
        entry_timeframe="15m",
    )
    account = AccountState(
        equity=equity_d,
        available_balance=avail_d,
        open_positions=[],
        today_realized_pnl=Decimal("0"),
        peak_equity=max(equity_d, Decimal("1000.0")),
        consecutive_losses=0,
        halt_state="normal",
        start_of_day_equity=equity_d,
    )
    market = make_valid_market(mark=entry_d, atr_15m=entry_d * Decimal("0.02"))
    config = RiskConfig()

    dec = evaluate(intent, account, market, config)

    assert dec.status in ("approved", "rejected")
    if dec.status == "approved":
        # Property invariants:
        # 1. Actual risk <= allowed risk
        assert dec.actual_risk_usd <= dec.allowed_risk_usd
        # 2. Leverage <= config.max_leverage
        assert dec.leverage <= config.max_leverage
        # 3. Quantity satisfies exchange filters
        assert dec.qty >= market.filters.min_qty
        # 4. Notional satisfies minNotional and cap
        assert dec.notional >= market.filters.min_notional
        assert dec.notional <= config.max_notional_mult * account.equity
        # 5. Liquidation strictly beyond stop by buffer
        req_buf = max(
            config.liq_buffer_atr_mult * (entry_d * Decimal("0.02")),
            config.liq_buffer_price_pct * entry_d,
        )
        assert dec.liquidation_price <= (intent.stop_loss - req_buf)


# ------------------------------------------------------------------------------
# 13. Hysteresis, Latching & Cooldown Extension Fixes
# ------------------------------------------------------------------------------


def test_drawdown_halt_latching_hysteresis_and_flapping_reproduction():
    """Confirms 15% drawdown halt latches with hysteresis:
    - Triggers at >= 15% drawdown.
    - Flapping reproduction: floating around 14.8%-15.2% does NOT repeatedly cycle transitions.
    - Does NOT clear if drawdown drops to 9.5% before 7 days have elapsed.
    - Does NOT clear after 7 days if drawdown is still >= 10%.
    - Clears to 'normal' ONLY when drawdown < 10% AND >= 7 days have elapsed.
    - Can also be cleared immediately via manual reset (reset_drawdown_halt).
    """
    config = RiskConfig(
        drawdown_halt_pct=Decimal("0.15"),
        drawdown_halt_recovery_pct=Decimal("0.10"),
        drawdown_halt_min_days=7,
    )
    tracker = CircuitBreakerTracker(config=config, initial_equity=Decimal("10000.0"))
    t0 = 1672531200000  # 2023-01-01 00:00:00 UTC
    one_day_ms = 86400 * 1000

    # 1. Day 1: Equity drops to $8,450 (15.5% drawdown) -> triggers drawdown_halt
    tracker.on_time_and_equity(t0, Decimal("8450.0"))
    assert tracker.halt_state == "drawdown_halt"
    assert tracker.drawdown_halt_entered_ms == t0
    assert len(tracker.transitions) == 1

    # 2. Flapping reproduction scenario:
    # Equity fluctuates around 14.5% - 15.2% over days 1 to 4:
    # Day 2: $8,520 (14.8% drawdown). Old unlatched code flapped back to 'normal'.
    # With hysteresis: must REMAIN in 'drawdown_halt'.
    tracker.on_time_and_equity(t0 + 1 * one_day_ms, Decimal("8520.0"))
    assert tracker.halt_state == "drawdown_halt"
    assert len(tracker.transitions) == 1  # No flapping transition!

    # Day 3: $8,480 (15.2% drawdown).
    tracker.on_time_and_equity(t0 + 2 * one_day_ms, Decimal("8480.0"))
    assert tracker.halt_state == "drawdown_halt"
    assert len(tracker.transitions) == 1  # Still no extra transition!

    # Day 4: $8,550 (14.5% drawdown).
    tracker.on_time_and_equity(t0 + 3 * one_day_ms, Decimal("8550.0"))
    assert tracker.halt_state == "drawdown_halt"
    assert len(tracker.transitions) == 1

    # 3. Day 5 (t0 + 4 days): Equity improves to $9,100 (9.0% drawdown, < 10%).
    # BUT only 4 days have elapsed (< 7 days required)!
    # Must REMAIN in 'drawdown_halt'.
    tracker.on_time_and_equity(t0 + 4 * one_day_ms, Decimal("9100.0"))
    assert tracker.halt_state == "drawdown_halt"
    assert len(tracker.transitions) == 1

    # 4. Day 8 (t0 + 7 days): 7 days elapsed, but equity dipped to $8,900 (11% drawdown, >= 10%).
    # Must REMAIN in 'drawdown_halt'.
    tracker.on_time_and_equity(t0 + 7 * one_day_ms, Decimal("8900.0"))
    assert tracker.halt_state == "drawdown_halt"
    assert len(tracker.transitions) == 1

    # 5. Day 8.5 (t0 + 7.5 days): 7+ days elapsed AND equity reaches $9,100 (9% drawdown, < 10%).
    # Clears to 'normal'!
    t_clear = t0 + int(7.5 * one_day_ms)
    tracker.on_time_and_equity(t_clear, Decimal("9100.0"))
    assert tracker.halt_state == "normal"
    assert tracker.drawdown_halt_entered_ms == 0
    assert len(tracker.transitions) == 2
    assert tracker.transitions[1].to_state == "normal"
    assert "Drawdown halt cleared" in tracker.transitions[1].reason

    # 6. Manual reset test:
    # Enter drawdown halt again at t_new
    t_new = t_clear + one_day_ms
    tracker.on_time_and_equity(t_new, Decimal("8400.0"))  # 16% drawdown
    assert tracker.halt_state == "drawdown_halt"
    # Call manual reset: clears immediately even if 0 days elapsed
    res = tracker.reset_drawdown_halt(t_new + 3600_000)
    assert res is True
    assert tracker.halt_state == "normal"


def test_half_risk_state_hysteresis():
    """Confirms 10% half-risk state has hysteresis:
    - Activates at >= 10% drawdown (is_risk_halved = True, risk halved in evaluate).
    - Remains active when recovering to 8.0% drawdown (>= 7%).
    - Deactivates (is_risk_halved = False) only when recovering strictly below 7%.
    """
    config = RiskConfig(
        drawdown_halve_risk_pct=Decimal("0.10"),
        drawdown_halve_risk_recovery_pct=Decimal("0.07"),
    )
    tracker = CircuitBreakerTracker(config=config, initial_equity=Decimal("10000.0"))
    t = 1672531200000

    # 1. 5% drawdown ($9,500): Normal risk (1%)
    tracker.on_time_and_equity(t, Decimal("9500.0"))
    assert tracker.is_risk_halved is False
    acct = tracker.get_account_state(equity=Decimal("9500.0"), available_balance=Decimal("9500.0"))
    intent = make_valid_intent()
    market = make_valid_market()
    dec = evaluate(intent, acct, market, config)
    assert dec.effective_risk_pct == Decimal("0.01")

    # 2. 10.5% drawdown ($8,950): Half-risk activated (0.5%)
    tracker.on_time_and_equity(t + 3600_000, Decimal("8950.0"))
    assert tracker.is_risk_halved is True
    acct = tracker.get_account_state(equity=Decimal("8950.0"), available_balance=Decimal("8950.0"))
    dec = evaluate(intent, acct, market, config)
    assert dec.effective_risk_pct == Decimal("0.005")
    assert any(r.code == ReasonCode.CIRCUIT_BREAKER_DRAWDOWN_RISK_HALVED for r in dec.reasons)

    # 3. Recovers to 8.0% drawdown ($9,200): Hysteresis keeps half-risk active!
    tracker.on_time_and_equity(t + 7200_000, Decimal("9200.0"))
    assert tracker.is_risk_halved is True
    acct = tracker.get_account_state(equity=Decimal("9200.0"), available_balance=Decimal("9200.0"))
    dec = evaluate(intent, acct, market, config)
    assert dec.effective_risk_pct == Decimal("0.005")

    # 4. Recovers to 6.5% drawdown ($9,350, < 7%): Half-risk clears back to normal!
    tracker.on_time_and_equity(t + 10800_000, Decimal("9350.0"))
    assert tracker.is_risk_halved is False
    acct = tracker.get_account_state(equity=Decimal("9350.0"), available_balance=Decimal("9350.0"))
    dec = evaluate(intent, acct, market, config)
    assert dec.effective_risk_pct == Decimal("0.01")


def test_cooldown_timer_not_extended_by_subsequent_losses():
    """Confirms root cause fix for 51h cooldown:
    Subsequent losses during an active streak cooldown do NOT push the cooldown timer forward.
    The cooldown timer expires 24h from the 4th loss, resetting consecutive_losses to 0.
    """
    config = RiskConfig(
        consecutive_losses_threshold=4,
        consecutive_losses_cooldown_hours=24,
    )
    tracker = CircuitBreakerTracker(config=config, initial_equity=Decimal("10000.0"))
    t0 = 1672531200000
    one_hour_ms = 3600 * 1000

    # 4 consecutive losses of $10 (total $40, well within 3% daily limit):
    tracker.record_trade_exit(Decimal("-10.0"), t0)
    tracker.record_trade_exit(Decimal("-10.0"), t0 + 1 * one_hour_ms)
    tracker.record_trade_exit(Decimal("-10.0"), t0 + 2 * one_hour_ms)
    tracker.record_trade_exit(Decimal("-10.0"), t0 + 3 * one_hour_ms)

    assert tracker.consecutive_losses == 4
    assert tracker.halt_state == "streak_cooldown"
    expected_cooldown_end = (t0 + 3 * one_hour_ms) + (24 * one_hour_ms)
    assert tracker.cooldown_until_ms == expected_cooldown_end

    # In shadow mode or with in-flight trades, loss #5 occurs 10 hours later
    t_loss5 = t0 + 13 * one_hour_ms
    tracker.record_trade_exit(Decimal("-10.0"), t_loss5)
    assert tracker.consecutive_losses == 5
    # The cooldown timer MUST NOT be pushed to t_loss5 + 24h!
    assert tracker.cooldown_until_ms == expected_cooldown_end

    # Loss #6 occurs 20 hours later
    t_loss6 = t0 + 23 * one_hour_ms
    tracker.record_trade_exit(Decimal("-10.0"), t_loss6)
    assert tracker.consecutive_losses == 6
    assert tracker.cooldown_until_ms == expected_cooldown_end

    # When expected_cooldown_end arrives, cooldown clears and consecutive_losses resets to 0
    tracker.on_time_and_equity(expected_cooldown_end, Decimal("9940.0"))
    assert tracker.halt_state == "normal"
    assert tracker.cooldown_until_ms == 0
    assert tracker.consecutive_losses == 0
