"""Shared signal helpers for the classic indicator strategies in `strategy_lab/strategies/`.

The eight classic strategies (SMA/EMA crossover, RSI reversal, MACD, Bollinger, breakout,
mean reversion, momentum) were ported from the "backtesting-trading-strategies" skill. Their
entry/exit RULES are kept; the execution is Deskmind's:

- Signals are computed on closed bars only and act at the NEXT bar's open (the skill filled at
  the signal bar's own close, which is look-ahead).
- Every entry carries a protective stop at `sl_atr_mult` x ATR (Deskmind requires an exchange-side
  stop on every position, and Strategy Lab measures results in R = distance to that stop).
- Indicators are the Pine-exact ones in `indicators.py` (TradingView parity).

A strategy computes four boolean columns in `indicators()` (`long_entry`, `short_entry`,
`long_exit`, `short_exit`) plus `atr`, and calls `act_on_signals()` from `on_bar()`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker

# Execution defaults shared with tas_v1 (sizing, fees, slippage) + the protective stop.
COMMON_PARAMS: dict[str, float | int | bool] = {
    "risk_atr_len": 14,
    "sl_atr_mult": 2.0,  # protective stop distance in ATRs
    "allow_reverse": True,
    "initial_capital": 10_000.0,
    "equity_pct_per_trade": 10.0,
    "commission_pct": 0.05,
    "slippage_ticks": 1,
}


def _prev(x: np.ndarray) -> np.ndarray:
    out = np.roll(np.asarray(x, dtype=float), 1)
    out[0] = np.nan
    return out


def crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pine ta.crossover: a[1] <= b[1] and a > b. Anything involving NaN is False."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    pa, pb = _prev(a), _prev(b)
    with np.errstate(invalid="ignore"):
        hit = (pa <= pb) & (a > b)
    return np.where(np.isnan(a) | np.isnan(b) | np.isnan(pa) | np.isnan(pb), False, hit)


def crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pine ta.crossunder: a[1] >= b[1] and a < b. Anything involving NaN is False."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    pa, pb = _prev(a), _prev(b)
    with np.errstate(invalid="ignore"):
        hit = (pa >= pb) & (a < b)
    return np.where(np.isnan(a) | np.isnan(b) | np.isnan(pa) | np.isnan(pb), False, hit)


def stdev(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.stdev (population standard deviation over n bars)."""
    return pd.Series(x).rolling(n, min_periods=n).std(ddof=0).to_numpy()


def roc(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.roc: percent change versus the close n bars ago."""
    x = np.asarray(x, dtype=float)
    past = pd.Series(x).shift(n).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(past > 0, (x - past) / past * 100.0, np.nan)


def act_on_signals(i: int, ctx: BarContext, broker: TradingViewBroker, params: dict) -> None:
    """Turn the signal columns of bar i into orders for bar i+1's open.

    - Long/short entry while flat: enter with a stop at sl_atr_mult x ATR.
    - Opposite entry while in a position: reverse (if allow_reverse), else just close.
    - Exit signal for the current side: close. A same-side entry while already in: hold.
    - New entries only inside the backtest window and when the coin passes the 30-day
      liquidity filter; exits are never blocked.
    """
    d = ctx.df
    long_entry = bool(d["long_entry"].iat[i])
    short_entry = bool(d["short_entry"].iat[i])
    want = 1 if long_entry else (-1 if short_entry else 0)

    can_enter = ctx.in_date
    if broker.min_quote_volume_30d is not None and ctx.vol_30d_med is not None:
        vol = ctx.vol_30d_med
        can_enter = can_enter and not np.isnan(vol) and vol >= broker.min_quote_volume_30d

    a = float(d["atr"].iat[i])
    stop_dist = a * float(params["sl_atr_mult"])
    has_stop = not np.isnan(stop_dist) and stop_dist > 0

    def enter(side: int) -> None:
        broker.entry(side=side, init_stop=ctx.close - side * stop_dist, current_close=ctx.close)

    if broker.is_long:
        if want == -1:
            if can_enter and has_stop and params.get("allow_reverse", True):
                enter(-1)
            else:
                broker.close(reason="Short Signal")
        elif bool(d["long_exit"].iat[i]):
            broker.close(reason="Exit Signal")
    elif broker.is_short:
        if want == 1:
            if can_enter and has_stop and params.get("allow_reverse", True):
                enter(1)
            else:
                broker.close(reason="Long Signal")
        elif bool(d["short_exit"].iat[i]):
            broker.close(reason="Exit Signal")
    elif want != 0 and can_enter and has_stop:
        enter(want)
