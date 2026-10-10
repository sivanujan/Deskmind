"""
TPRE - Trend Pullback Re-Entry Strategy (Python port of the Pine v6 script
"Trend Pullback Re-Entry Strategy [Jayadev Rana]").

Default settings only (what the TradingView strategy uses out of the box):

  Trend regime
    up   = EMA55 > EMA233 and EMA233 > EMA233[10 bars ago]
    down = EMA55 < EMA233 and EMA233 < EMA233[10 bars ago]
  Pullback + trigger
    long : uptrend, RSI(5) <= 25 happened within the last 3 bars (incl. now),
           and close > previous bar's high
    short: downtrend, RSI(5) >= 75 within the last 3 bars,
           and close < previous bar's low
    only when flat; warm-up = bar_index > 243
  Risk
    initial stop = lowest low of last 4 bars - 0.5 x ATR14  (long)
                   highest high of last 4 bars + 0.5 x ATR14 (short)
    size = 0.5 % of equity at risk / stop distance, capped at 1x equity
    after entry the stop trails each close by 5 x ATR14 and never loosens
    (it starts at the initial stop, so it only moves once
     close - 5 ATR rises above it). No take-profit.
  Costs: commission 0.05 % per fill, slippage 2 ticks (market + stop fills)

Broker rules = TradingView: script runs on bar close, market orders fill at
the next bar open, stop orders are live from the next bar (including the
entry bar), a gap through the stop fills at the open.

VERIFY (needs TradingView parity): Pine's built-in ta.ema seeding. This
port seeds with the SMA of the first `length` values; with years of
warm-up history before the test window the difference is negligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TPREParams:
    fast_len: int = 55
    slow_len: int = 233
    slope_len: int = 10
    pb_len: int = 5
    pb_level: float = 25.0
    pb_window: int = 3
    allow_long: bool = True
    allow_short: bool = True
    risk_pct: float = 0.5
    max_lev: float = 1.0
    stop_buf: float = 0.5
    trail_mult: float = 5.0
    atr_len: int = 14
    initial_capital: float = 10_000.0
    commission_pct: float = 0.05
    slippage_ticks: int = 2


# ── Pine-exact indicators (NaN = na) ─────────────────────────────────────────
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
            prev = alpha * (0.0 if np.isnan(v) else v) + (1 - alpha) * prev
            out[i] = prev
    return out


def ema(x, n):
    return _sma_seeded_recursive(x, n, 2.0 / (n + 1))


def rma(x, n):
    return _sma_seeded_recursive(x, n, 1.0 / n)


def true_range(h, l, c):
    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    tr[0] = h[0] - l[0]                      # ta.tr(true) on the first bar
    return tr


def rsi(x, n):
    d = np.r_[np.nan, np.diff(x)]
    up = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
    dn = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
    ru, rd = rma(up, n), rma(dn, n)
    out = np.full(len(x), np.nan)
    ok = ~(np.isnan(ru) | np.isnan(rd))
    with np.errstate(divide="ignore", invalid="ignore"):
        val = 100.0 - 100.0 / (1.0 + ru / rd)
    out[ok] = val[ok]
    out[ok & (rd == 0)] = 100.0
    out[ok & (ru == 0) & (rd != 0)] = 0.0
    return out


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


def rolling_min(x, n):
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy()


def rolling_max(x, n):
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy()


def compute_indicators(df: pd.DataFrame, p: TPREParams, tick: float) -> pd.DataFrame:
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    out = df.copy()
    ema_f, ema_s = ema(c, p.fast_len), ema(c, p.slow_len)
    ema_s_lag = np.r_[np.full(p.slope_len, np.nan), ema_s[:-p.slope_len]]
    with np.errstate(invalid="ignore"):
        up = (ema_f > ema_s) & (ema_s > ema_s_lag)
        dn = (ema_f < ema_s) & (ema_s < ema_s_lag)
    r = rsi(c, p.pb_len)
    atr = rma(true_range(h, l, c), p.atr_len)
    unit = np.maximum(np.where(np.isnan(atr), h - l, atr), tick)
    with np.errstate(invalid="ignore"):
        since_dip = np.nan_to_num(barssince(r <= p.pb_level), nan=100000)
        since_pop = np.nan_to_num(barssince(r >= 100 - p.pb_level), nan=100000)
    out["ema_f"], out["ema_s"] = ema_f, ema_s
    out["up_trend"], out["dn_trend"] = up, dn
    out["rsi_p"], out["atr"], out["unit"] = r, atr, unit
    out["since_dip"], out["since_pop"] = since_dip, since_pop
    out["dip_low"] = rolling_min(l, p.pb_window + 1)
    out["pop_high"] = rolling_max(h, p.pb_window + 1)
    return out


# ── Backtest (TradingView broker rules) ─────────────────────────────────────
def run_backtest(df: pd.DataFrame, tick: float,
                 params: TPREParams = TPREParams(),
                 start: Optional[str] = None,
                 end: Optional[str] = None) -> dict:
    """df: full history (indicators warm up on everything), columns
    open_time (UTC), open, high, low, close. Entries only inside
    [start, end]; an open position is closed at the first bar open after
    `end`. tick: exchange tick size (required)."""
    if tick is None or tick <= 0:
        raise ValueError("tick size is required")
    p = params
    d = compute_indicators(df.reset_index(drop=True), p, tick)
    t = pd.to_datetime(d["open_time"], utc=True)
    O, H, L, C = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    up, dn = d["up_trend"].to_numpy(), d["dn_trend"].to_numpy()
    sdip, spop = d["since_dip"].to_numpy(), d["since_pop"].to_numpy()
    dip_low, pop_high = d["dip_low"].to_numpy(), d["pop_high"].to_numpy()
    unit = d["unit"].to_numpy()
    t0 = pd.Timestamp(start, tz="UTC") if start else None
    t1 = pd.Timestamp(end, tz="UTC") if end else None
    slip = p.slippage_ticks * tick
    fee = p.commission_pct / 100.0
    n = len(d)

    realized = 0.0
    pos = None                 # dict while in a trade
    pending = None             # entry to fill at next open
    live_stop = np.nan         # stop order live from next bar
    trail = np.nan             # Pine `var float trail`
    close_at_open = False
    rows, eq_curve = [], []

    def exit_pos(i, price, reason):
        nonlocal pos, realized
        q = pos["qty"]
        exit_fee = price * q * fee
        pnl = pos["side"] * (price - pos["entry_price"]) * q - pos["entry_fee"] - exit_fee
        realized += pnl
        risk = abs(pos["plan_entry"] - pos["init_stop"]) * q
        rows.append({
            "side": "long" if pos["side"] == 1 else "short",
            "entry_time": pos["entry_time"], "entry_price": pos["entry_price"],
            "exit_time": t[i], "exit_price": price, "qty": q,
            "init_stop": pos["init_stop"], "plan_entry": pos["plan_entry"],
            "fees": pos["entry_fee"] + exit_fee, "pnl": pnl, "risk_usd": risk,
            "r_multiple": pnl / risk if risk > 0 else np.nan,
            "exit_reason": reason, "bars_held": i - pos["entry_bar"],
        })
        pos = None

    for i in range(n):
        # 1. market orders at the open
        if close_at_open and pos is not None:
            exit_pos(i, O[i] - pos["side"] * slip, "Window End")
        close_at_open = False
        if pending is not None:
            s = pending["side"]
            price = O[i] + s * slip
            pos = {"side": s, "qty": pending["qty"], "entry_price": price,
                   "entry_fee": price * pending["qty"] * fee,
                   "entry_time": t[i], "entry_bar": i,
                   "plan_entry": pending["plan_entry"],
                   "init_stop": pending["stop"]}
            pending = None

        # 2. stop order (live since previous close)
        if pos is not None and not np.isnan(live_stop):
            s = pos["side"]
            if (s == 1 and O[i] <= live_stop) or (s == -1 and O[i] >= live_stop):
                exit_pos(i, O[i] - s * slip, "Stop (gap)")
            elif (s == 1 and L[i] <= live_stop) or (s == -1 and H[i] >= live_stop):
                exit_pos(i, live_stop - s * slip, "Stop")

        # 3. script on bar close
        flat = pos is None
        in_date = (t0 is None or t[i] >= t0) and (t1 is None or t[i] <= t1)
        warm = i > p.slow_len + p.slope_len
        long_sig = (warm and flat and p.allow_long and up[i]
                    and sdip[i] <= p.pb_window and i > 0 and C[i] > H[i - 1])
        short_sig = (warm and flat and p.allow_short and dn[i]
                     and spop[i] <= p.pb_window and i > 0 and C[i] < L[i - 1])
        long_sig, short_sig = long_sig and in_date, short_sig and in_date

        equity = p.initial_capital + realized   # flat when a signal fires

        def qty_for(stop_dist):
            q = equity * p.risk_pct / 100 / stop_dist if stop_dist > 0 else 0.0
            cap = equity * p.max_lev / C[i]
            return max(min(q, cap), 0.0)

        if long_sig:
            stop = dip_low[i] - p.stop_buf * unit[i]
            q = qty_for(C[i] - stop)
            if q > 0:
                trail = stop
                pending = {"side": 1, "qty": q, "stop": stop, "plan_entry": C[i]}
                live_stop = trail
        elif short_sig:
            stop = pop_high[i] + p.stop_buf * unit[i]
            q = qty_for(stop - C[i])
            if q > 0:
                trail = stop
                pending = {"side": -1, "qty": q, "stop": stop, "plan_entry": C[i]}
                live_stop = trail
        elif pos is not None and pos["side"] == 1:
            cand = C[i] - p.trail_mult * unit[i]
            trail = max(cand if np.isnan(trail) else trail, cand)
            live_stop = trail
        elif pos is not None and pos["side"] == -1:
            cand = C[i] + p.trail_mult * unit[i]
            trail = min(cand if np.isnan(trail) else trail, cand)
            live_stop = trail
        else:
            trail = np.nan
            if pending is None:
                live_stop = np.nan

        if t1 is not None and t[i] > t1 and pos is not None:
            close_at_open = True

        opnl = 0.0 if pos is None else pos["side"] * (C[i] - pos["entry_price"]) * pos["qty"]
        eq_curve.append(p.initial_capital + realized + opnl)

    trades = pd.DataFrame(rows)
    eq = pd.Series(eq_curve, index=t)
    return {"trades": trades, "positions": trades, "equity": eq,
            "stats": summarize(trades, eq, p.initial_capital)}


def summarize(trades: pd.DataFrame, equity: pd.Series, capital: float) -> dict:
    if trades.empty:
        return {"closed_trades": 0, "positions": 0}
    w = trades[trades["pnl"] > 0]
    lo = trades[trades["pnl"] <= 0]
    gp, gl = w["pnl"].sum(), -lo["pnl"].sum()
    peak = equity.cummax()
    dd = peak - equity
    r = trades["r_multiple"].dropna()
    return {
        "closed_trades": int(len(trades)),
        "positions": int(len(trades)),
        "win_rate_pct": round(100 * len(w) / len(trades), 2),
        "net_profit": round(trades["pnl"].sum(), 2),
        "net_profit_pct": round(100 * trades["pnl"].sum() / capital, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else float("inf"),
        "max_drawdown": round(float(dd.max()), 2),
        "max_drawdown_pct": round(float((dd / peak).max() * 100), 2),
        "mean_r": round(float(r.mean()), 4),
        "median_r": round(float(r.median()), 4),
        "avg_bars_held": round(float(trades["bars_held"].mean()), 1),
        "cost_r": round(float((trades["fees"] / trades["risk_usd"]).mean()), 4),
    }
