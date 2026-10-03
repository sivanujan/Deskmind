"""Shared execution fill and slippage model for backtest and paper execution.

Both the backtest engine and the paper executor MUST use this identical code to
guarantee that simulated execution behavior, slippage, and fee drag match.
"""

from __future__ import annotations

from decimal import Decimal

from deskmind.backtest.filters import round_price


def calc_slippage_pct(
    price: Decimal,
    atr: float | Decimal | None,
    slippage_bps: Decimal = Decimal("2.0"),
    slippage_atr_factor: Decimal = Decimal("0.05"),
) -> Decimal:
    """Calculate slippage percentage = fixed_bps + atr_factor * (atr / price)."""
    base = slippage_bps * Decimal("0.0001")
    if atr is not None and atr > 0 and price > Decimal("0"):
        dynamic = slippage_atr_factor * (Decimal(str(atr)) / price)
        return base + dynamic
    return base


def simulate_market_fill(
    price: Decimal,
    side: str,  # "BUY", "SELL", "long", or "short"
    tick_size: Decimal,
    atr: float | Decimal | None = None,
    slippage_bps: Decimal = Decimal("2.0"),
    slippage_atr_factor: Decimal = Decimal("0.05"),
) -> tuple[Decimal, Decimal]:
    """Calculate market order fill price after applying slippage model and tick rounding.

    Returns (fill_price, slip_pct).
    """
    slip_pct = calc_slippage_pct(price, atr, slippage_bps, slippage_atr_factor)
    if side.upper() in ("BUY", "LONG"):
        raw_price = price * (Decimal("1") + slip_pct)
    else:
        raw_price = price * (Decimal("1") - slip_pct)
    return round_price(raw_price, tick_size), slip_pct


def simulate_candle_exit(
    pos_side: str,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    b_open: Decimal,
    b_high: Decimal,
    b_low: Decimal,
    tick_size: Decimal,
    atr: float | Decimal | None = None,
    slippage_bps: Decimal = Decimal("2.0"),
    slippage_atr_factor: Decimal = Decimal("0.05"),
) -> tuple[Decimal | None, str | None, bool, Decimal]:
    """Evaluate protective stop-loss and take-profit triggers on a candle bar.

    Follows strict backtest simulation invariants:
    1. Gap through stop at bar open executes at open price with slippage.
    2. If both SL and TP are touched in the same bar, conservative assumption: SL hit first.
    3. Take profit executes at limit target price (maker).
    4. Stop loss executes at stop price minus/plus slippage (taker).

    Returns (exit_price, exit_reason, is_maker, slip_pct).
    If no trigger occurred, returns (None, None, False, Decimal("0")).
    """
    slip_pct = calc_slippage_pct(b_open, atr, slippage_bps, slippage_atr_factor)
    pos_side = pos_side.lower()

    if pos_side == "long":
        # Gap check: opened below stop
        if stop_loss is not None and b_open <= stop_loss:
            raw_exit = b_open * (Decimal("1") - slip_pct)
            return round_price(raw_exit, tick_size), "stop_loss", False, slip_pct

        # Both SL and TP hit in same bar -> conservative: SL hit first
        # TP limit exit requires trade-through by >= 1 tick (b_high >= take_profit + tick_size)
        both_hit = (
            stop_loss is not None
            and take_profit is not None
            and b_low <= stop_loss
            and b_high >= (take_profit + tick_size)
        )
        if both_hit:
            assert stop_loss is not None
            raw_exit = stop_loss * (Decimal("1") - slip_pct)
            return round_price(raw_exit, tick_size), "sl_tp_same_bar", False, slip_pct

        if stop_loss is not None and b_low <= stop_loss:
            raw_exit = stop_loss * (Decimal("1") - slip_pct)
            return round_price(raw_exit, tick_size), "stop_loss", False, slip_pct

        # Target exit is a maker limit: fills ONLY if traded through by >= 1 tick (touch = no fill)
        if take_profit is not None and b_high >= (take_profit + tick_size):
            return take_profit, "take_profit", True, Decimal("0")

    elif pos_side == "short":
        # Gap check: opened above stop
        if stop_loss is not None and b_open >= stop_loss:
            raw_exit = b_open * (Decimal("1") + slip_pct)
            return round_price(raw_exit, tick_size), "stop_loss", False, slip_pct

        # Both SL and TP hit in same bar -> conservative: SL hit first
        # TP limit exit requires trade-through by >= 1 tick (b_low <= take_profit - tick_size)
        both_hit_short = (
            stop_loss is not None
            and take_profit is not None
            and b_high >= stop_loss
            and b_low <= (take_profit - tick_size)
        )
        if both_hit_short:
            assert stop_loss is not None
            raw_exit = stop_loss * (Decimal("1") + slip_pct)
            return round_price(raw_exit, tick_size), "sl_tp_same_bar", False, slip_pct

        if stop_loss is not None and b_high >= stop_loss:
            raw_exit = stop_loss * (Decimal("1") + slip_pct)
            return round_price(raw_exit, tick_size), "stop_loss", False, slip_pct

        # Target exit is a maker limit: fills ONLY if traded through by >= 1 tick (touch = no fill)
        if take_profit is not None and b_low <= (take_profit - tick_size):
            return take_profit, "take_profit", True, Decimal("0")

    return None, None, False, Decimal("0")


def check_limit_trade_through(
    side: str,
    limit_price: Decimal,
    b_high: Decimal,
    b_low: Decimal,
    tick_size: Decimal,
) -> bool:
    """Check honest trade-through condition for a limit order on a single candle.

    Touch-only is NEVER a fill:
    - Long (buy): requires b_low <= limit_price - tick_size (trades through by >= 1 tick).
      If b_low == limit_price, returns False.
    - Short (sell): requires b_high >= limit_price + tick_size (trades through by >= 1 tick).
      If b_high == limit_price, returns False.
    """
    if side.lower() in ("long", "buy"):
        return b_low <= (limit_price - tick_size)
    else:
        return b_high >= (limit_price + tick_size)


def simulate_limit_fill(
    side: str,
    limit_price: Decimal,
    b_high: Decimal,
    b_low: Decimal,
    tick_size: Decimal,
) -> tuple[Decimal | None, bool]:
    """Evaluate honest trade-through limit fill on a single candle.

    Returns (fill_price, is_maker).
    If filled: (limit_price, True) with maker execution.
    If not filled: (None, False).
    """
    if check_limit_trade_through(side, limit_price, b_high, b_low, tick_size):
        return limit_price, True
    return None, False

