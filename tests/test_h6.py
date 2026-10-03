"""Unit tests for Hypothesis H6_v1.0.0 (4h Trend Breakout).

Covers:
- Frozen spec hash verification and program-wide trial count K=10.
- Resampling correctness (48 5m bars -> 1 4h bar with exact OHLCV).
- No look-ahead (unclosed 4h bars do not emit signals).
- One signal per breakout (no duplicate signals during an ongoing breakout).
- 10-day time exit trigger.
"""

from decimal import Decimal
import pytest

from deskmind.research.hypotheses.registry import (
    H6_FROZEN_SHA256,
    REGISTRY,
    verify_h6_spec_hash,
)
from deskmind.strategies.base import (
    AccountSnapshot,
    BarSnapshot,
    PositionSnapshot,
    StrategyContext,
)
from deskmind.strategies.h6_trend_breakout import (
    FOUR_HOURS_MS,
    TEN_DAYS_MS,
    H6TrendBreakoutStrategy,
)


def test_h6_frozen_spec_hash_and_registry_program_trial_count_is_10() -> None:
    """H6 spec hash matches pinned SHA-256 and program trials count is exactly 10."""
    assert verify_h6_spec_hash() is True
    assert REGISTRY.get_program_trial_count() == 10
    trials = REGISTRY.get_program_trials()
    assert len(trials) == 10
    assert "H6_v1.0.0" in trials


def test_h6_resample_correctness() -> None:
    """48 consecutive 5m bars resample to exactly 1 4h bar with correct OHLCV."""
    strategy = H6TrendBreakoutStrategy()
    sym = "BTCUSDT"
    t0 = 1672531200000  # 2023-01-01 00:00:00 UTC (4h boundary)

    highs = []
    lows = []
    volumes = []

    for i in range(48):
        b_open_time = t0 + i * 300_000
        b_close_time = b_open_time + 300_000
        o = 100.0 + i
        h = 105.0 + i
        l = 95.0 + i
        c = 101.0 + i
        v = 10.0 + i

        highs.append(h)
        lows.append(l)
        volumes.append(v)

        bar = BarSnapshot(
            open_time=b_open_time,
            close_time=b_close_time,
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(l)),
            close=Decimal(str(c)),
            volume=Decimal(str(v)),
        )
        ctx = StrategyContext(
            symbol=sym,
            as_of_ms=b_close_time,
            current_bar=bar,
            agent_report=None,
            account=None,
        )
        strategy.on_bar({sym: ctx})

    # At bar 48 (i=47), the 4h bar closed
    assert sym in strategy._closed_4h
    closed_bars = strategy._closed_4h[sym]
    assert len(closed_bars) == 1

    b4h = closed_bars[0]
    assert b4h.open_time == t0
    assert b4h.close_time == t0 + FOUR_HOURS_MS
    assert b4h.open == 100.0  # Open of first bar
    assert b4h.high == max(highs)  # Max high
    assert b4h.low == min(lows)  # Min low
    assert b4h.close == 101.0 + 47  # Close of last bar
    assert abs(b4h.volume - sum(volumes)) < 1e-4  # Sum of volumes


def test_h6_no_look_ahead() -> None:
    """Strategy evaluates signals strictly at 4h close boundaries, never during unclosed bars."""
    strategy = H6TrendBreakoutStrategy()
    sym = "BTCUSDT"
    t0 = 1672531200000

    # Feed 47 bars (unclosed 4h bar) with massive breakout price
    for i in range(47):
        b_open_time = t0 + i * 300_000
        b_close_time = b_open_time + 300_000
        bar = BarSnapshot(
            open_time=b_open_time,
            close_time=b_close_time,
            open=Decimal("1000.0"),
            high=Decimal("2000.0"),
            low=Decimal("900.0"),
            close=Decimal("1900.0"),
            volume=Decimal("100.0"),
        )
        ctx = StrategyContext(
            symbol=sym,
            as_of_ms=b_close_time,
            current_bar=bar,
            agent_report=None,
            account=None,
        )
        intents = strategy.on_bar({sym: ctx})
        assert len(intents) == 0, f"Unclosed bar at step {i} must never emit intents"
        assert len(strategy._closed_4h.get(sym, [])) == 0


def test_h6_one_signal_per_breakout() -> None:
    """Only one signal is emitted per breakout; subsequent bars in the same breakout emit 0."""
    strategy = H6TrendBreakoutStrategy()
    sym = "BTCUSDT"
    t0 = 1672531200000

    # Build 55 closed 4h bars:
    # 54 baseline bars with prices oscillating around 100
    # Then bar 55 breaks out upwards with close=150 (prev 20 high was ~110)
    # Then bar 56 continues upwards with close=160
    # Then bar 57 closes back down at 100 (resets breakout)
    # Then bar 58 breaks out upwards again at 170

    def feed_4h_bar(bar_idx: int, c_price: float, h_price: float, l_price: float) -> list:
        start_4h = t0 + bar_idx * FOUR_HOURS_MS
        intents = []
        for i in range(48):
            open_ms = start_4h + i * 300_000
            close_ms = open_ms + 300_000
            # On the last 5m bar, apply the specified 4h close/high/low
            is_last = (i == 47)
            bar = BarSnapshot(
                open_time=open_ms,
                close_time=close_ms,
                open=Decimal(str(c_price)),
                high=Decimal(str(h_price if is_last else c_price)),
                low=Decimal(str(l_price if is_last else c_price)),
                close=Decimal(str(c_price)),
                volume=Decimal("10.0"),
            )
            ctx = StrategyContext(
                symbol=sym,
                as_of_ms=close_ms,
                current_bar=bar,
                agent_report=None,
                account=None,
            )
            its = strategy.on_bar({sym: ctx})
            if its:
                intents.extend(its)
        return intents

    # 1. Warm up with 54 rising bars (so EMA50 is rising)
    for b in range(54):
        # Gently ascending prices so EMA50 is definitely rising
        p = 100.0 + b * 0.2
        feed_4h_bar(b, c_price=p, h_price=p + 2.0, l_price=p - 2.0)

    # 2. Bar 54 (idx=54): Initial breakout above 20-bar high
    # Previous 20 bars high was around 110.0 + 2.0 = 112.0
    intents_breakout = feed_4h_bar(54, c_price=130.0, h_price=132.0, l_price=128.0)
    assert len(intents_breakout) == 1, f"Expected 1 breakout intent, got {len(intents_breakout)}"
    assert intents_breakout[0].action == "open_long"

    # 3. Bar 55 (idx=55): Price goes even higher (140.0) -> MUST NOT emit another intent (in breakout)
    intents_continuation = feed_4h_bar(55, c_price=140.0, h_price=142.0, l_price=138.0)
    assert len(intents_continuation) == 0, f"Same breakout must emit 0 intents, got {len(intents_continuation)}"

    # 4. Bar 56 (idx=56): Price pulls back inside range (close=110.0, low=108.0) -> resets breakout
    intents_pullback = feed_4h_bar(56, c_price=110.0, h_price=115.0, l_price=108.0)
    assert len(intents_pullback) == 0

    # 5. Bar 57 (idx=57): Fresh breakout above the new 20-bar high (which was 142.0)
    intents_fresh = feed_4h_bar(57, c_price=150.0, h_price=152.0, l_price=148.0)
    assert len(intents_fresh) == 1, f"Expected fresh breakout intent, got {len(intents_fresh)}"
    assert intents_fresh[0].action == "open_long"


def test_h6_time_exit() -> None:
    """Position held for >= 10 days emits close intent with exit_reason = 'time_limit'."""
    strategy = H6TrendBreakoutStrategy()
    sym = "BTCUSDT"
    t_entry = 1672531200000

    pos = PositionSnapshot(
        symbol=sym,
        side="long",
        entry_price=Decimal("100.0"),
        qty=Decimal("1.0"),
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("110.0"),
        entry_time_ms=t_entry,
        liquidation_price=Decimal("80.0"),
    )
    acc = AccountSnapshot(
        cash=Decimal("10000.0"),
        equity=Decimal("10000.0"),
        realized_pnl=Decimal("0.0"),
        unrealized_pnl=Decimal("0.0"),
        open_position=pos,
    )

    # 1. Bar at 9 days: should NOT emit time exit
    t_9days = t_entry + 9 * 86_400_000
    bar_9d = BarSnapshot(
        open_time=t_9days,
        close_time=t_9days + 300_000,
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal("100.0"),
        volume=Decimal("10.0"),
    )
    ctx_9d = StrategyContext(
        symbol=sym,
        as_of_ms=t_9days + 300_000,
        current_bar=bar_9d,
        agent_report=None,
        account=acc,
    )
    intents_9d = strategy.on_bar({sym: ctx_9d})
    assert len(intents_9d) == 0

    # 2. Bar at 10 days (864,000,000 ms): MUST emit close intent with exit_reason = "time_limit"
    t_10days = t_entry + TEN_DAYS_MS
    bar_10d = BarSnapshot(
        open_time=t_10days,
        close_time=t_10days + 300_000,
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal("100.0"),
        volume=Decimal("10.0"),
    )
    ctx_10d = StrategyContext(
        symbol=sym,
        as_of_ms=t_10days + 300_000,
        current_bar=bar_10d,
        agent_report=None,
        account=acc,
    )
    intents_10d = strategy.on_bar({sym: ctx_10d})
    assert len(intents_10d) == 1
    assert intents_10d[0].action == "close"
    assert intents_10d[0].metadata.get("exit_reason") == "time_limit"
