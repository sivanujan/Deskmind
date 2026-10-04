"""Pine-exact indicator functions for Strategy Lab.

All functions take and return numpy float arrays; NaN represents Pine's `na`.
These implementations exactly match TradingView Pine Script v6 calculations.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Simple Moving Average."""
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


def wma(x: np.ndarray, n: int) -> np.ndarray:
    """Weighted Moving Average with weights [1, 2, ..., n]."""
    w = np.arange(1, n + 1, dtype=float)
    wsum = w.sum()
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        win = x[i - n + 1 : i + 1]
        if not np.isnan(win).any():
            out[i] = float(np.dot(win, w) / wsum)
    return out


def _sma_seeded_recursive(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    out = np.full(len(x), np.nan)
    prev = np.nan
    for i in range(len(x)):
        if np.isnan(prev):
            if i >= n - 1:
                win = x[i - n + 1 : i + 1]
                if not np.isnan(win).any():
                    prev = float(win.mean())
                    out[i] = prev
        else:
            v = x[i]
            prev = alpha * (0.0 if np.isnan(v) else v) + (1.0 - alpha) * prev
            out[i] = prev
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.ema (Exponential Moving Average seeded with SMA)."""
    return _sma_seeded_recursive(x, n, 2.0 / (n + 1))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.rma (Running Moving Average / Wilder's MA).

    Seeded with SMA of the first n values, then alpha = 1/n recursion:
    RMA[i] = alpha * x[i] + (1 - alpha) * RMA[i-1].
    """
    return _sma_seeded_recursive(x, n, 1.0 / n)


def tr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    handle_na: bool = True,
) -> np.ndarray:
    """True Range.

    max(high - low, abs(high - prev_close), abs(low - prev_close)).
    If handle_na=True, index 0 is high[0] - low[0]; otherwise NaN.
    """
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    true_range = np.maximum.reduce(
        [
            high - low,
            np.abs(high - prev_close),
            np.abs(low - prev_close),
        ]
    )
    if handle_na:
        true_range[0] = high[0] - low[0]
    else:
        true_range[0] = np.nan
    return true_range


def atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    n: int = 14,
    handle_na: bool = True,
) -> np.ndarray:
    """Average True Range: RMA(TR, n)."""
    return rma(tr(high, low, close, handle_na=handle_na), n)


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    """Relative Strength Index matching Pine ta.rsi (RMA of up/down changes)."""
    diff = np.diff(x, prepend=np.nan)
    up = np.where(np.isnan(diff), np.nan, np.maximum(diff, 0.0))
    dn = np.where(np.isnan(diff), np.nan, np.maximum(-diff, 0.0))
    ru, rd = rma(up, n), rma(dn, n)
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if np.isnan(ru[i]) or np.isnan(rd[i]):
            continue
        if rd[i] == 0:
            out[i] = 100.0
        elif ru[i] == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ru[i] / rd[i])
    return out


def fixnan(x: np.ndarray) -> np.ndarray:
    """Forward fill NaNs matching Pine fixnan()."""
    return pd.Series(x).ffill().to_numpy()


def dmi(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    n: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Directional Movement Index returning (plus_di, minus_di, adx)."""
    up = np.diff(high, prepend=np.nan)
    down = -np.diff(low, prepend=np.nan)
    plus_dm = np.where(np.isnan(up), np.nan, np.where((up > down) & (up > 0), up, 0.0))
    minus_dm = np.where(np.isnan(down), np.nan, np.where((down > up) & (down > 0), down, 0.0))
    trur = rma(tr(high, low, close, handle_na=False), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = fixnan(100.0 * rma(plus_dm, n) / trur)
        minus = fixnan(100.0 * rma(minus_dm, n) / trur)
        s = plus + minus
        adx = 100.0 * rma(np.abs(plus - minus) / np.where(s == 0, 1.0, s), n)
    return plus, minus, adx


def linreg(x: np.ndarray, n: int) -> np.ndarray:
    """Least-squares fitted value at the newest bar (offset 0) matching Pine ta.linreg."""
    out = np.full(len(x), np.nan)
    xs = np.arange(n, dtype=float)
    xm = xs.mean()
    sxx = ((xs - xm) ** 2).sum()
    for i in range(n - 1, len(x)):
        win = x[i - n + 1 : i + 1]
        if np.isnan(win).any():
            continue
        ym = win.mean()
        slope = ((xs - xm) * (win - ym)).sum() / sxx
        out[i] = ym + slope * (xs[-1] - xm)
    return out


def hma(x: np.ndarray, n: int) -> np.ndarray:
    """Hull Moving Average: WMA(2 * WMA(n/2) - WMA(n), floor(sqrt(n)))."""
    half = max(1, n // 2)
    root = max(1, int(math.sqrt(n)))
    return wma(2.0 * wma(x, half) - wma(x, n), root)


def tracker(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    n: int = 14,
    mult: float = 2.2,
) -> tuple[np.ndarray, np.ndarray]:
    """Supertrend-like ratchet tracker based on WMA of True Range.

    Returns:
        (tracker_level, direction) where direction is +1 (bullish) or -1 (bearish).
    """
    a = wma(tr(high, low, close, handle_na=True), n)
    up = high + a * mult
    dn = low - a * mult
    trk = np.full(len(close), np.nan)
    d = np.ones(len(close), dtype=int)
    t, direction = np.nan, 1
    for i in range(len(close)):
        if np.isnan(t):
            t = dn[i]
        elif direction == 1:
            t = max(t, dn[i])
            if close[i] < t:
                direction, t = -1, up[i]
        else:
            t = min(t, up[i])
            if close[i] > t:
                direction, t = 1, dn[i]
        trk[i], d[i] = t, direction
    return trk, d


def gt(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pine comparison: anything involving NaN evaluates to False."""
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(a) | np.isnan(b), False, a > b)


def barssince(cond: np.ndarray) -> np.ndarray:
    """Pine ta.barssince: 0 when true now, NaN if never true yet."""
    out = np.full(len(cond), np.nan)
    last = -1
    for i, c in enumerate(cond):
        if c:
            last = i
        if last >= 0:
            out[i] = i - last
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    """Rolling minimum over n bars."""
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy()


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    """Rolling maximum over n bars."""
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy()


# Backward-compatible aliases
_sma = sma
_wma = wma
_rma = rma
_ema = ema
_tr = tr
_atr = atr
_rsi = rsi
_fixnan = fixnan
_dmi = dmi
_linreg = linreg
_hma = hma
_tracker = tracker
_gt = gt
_barssince = barssince
_rolling_min = rolling_min
_rolling_max = rolling_max
true_range = tr
