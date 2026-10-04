"""
TAS - Trend Analysis Strategy (Python port of the TradingView Pine v6 script).

Ported for Deskmind research. Only the DEFAULT settings path of the Pine
script is implemented (that is the configuration the TradingView report
used):

  * Trend tracker: WMA(true range, 14) x 2.2 ratchet (Supertrend-like)
  * Trend score (6 components): tracker dir, price vs equilibrium,
    equilibrium slope, RSI vs 50, RSI vs RSI-SMA, +DI vs -DI
  * Entry filter: score >= 50 % (i.e. >= 3 of 6), all other filters OFF
  * Delayed entry allowed within 10 bars of the trend flip, once per trend
  * Initial stop = trend tracker; targets 1.5R / 2.5R / 3.5R
    (33 % / 33 % / remainder), stop -> breakeven after TP1
  * Exit on opposite trend flip, reverse on opposite signal
  * Sizing: 10 % of equity per entry (TradingView "Properties tab" default)
  * Commission 0.05 % per fill, slippage 1 tick on market/stop fills

Broker emulation follows TradingView's documented rules:
  * the script runs on bar close; market orders fill at the NEXT bar open
  * stop / limit exits are checked intrabar on later bars, using the
    TradingView price path: if high is closer to open than low,
    open -> high -> low -> close, otherwise open -> low -> high -> close
  * a gap through a stop/limit fills at the open
  * limit orders fill on touch (TradingView default), no slippage
  * every exit order fill is a separate "closed trade" row (TradingView
    style), so one position can produce up to 3 trade rows

No look-ahead: every decision on bar t uses only data up to and including
bar t's close; orders resulting from it execute from bar t+1 onwards.

Items marked VERIFY are TradingView internals that the parity test against
TradingView's own report must confirm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ════════════════════════════════════════════════════════════════════════════
# PARAMETERS (Pine defaults)
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TASParams:
    trend_atr_len: int = 14
    trend_mult: float = 2.2
    rsi_len: int = 14
    rsi_smooth: int = 14
    dmi_len: int = 14
    eq_len: int = 55
    eq_smooth: int = 9
    min_score_pct: float = 50.0
    allow_reverse: bool = True
    allow_late: bool = True
    late_max: int = 10
    direction: str = "Both"            # "Both" | "Long Only" | "Short Only"
    risk_atr_len: int = 14
    sl_atr_mult: float = 2.0           # only used by the stop-side guard
    be_after_tp1: bool = True
    tp1_r: float = 1.5
    tp1_qty_pct: float = 33.0
    tp2_r: float = 2.5
    tp2_qty_pct: float = 33.0
    tp3_r: float = 3.5
    exit_on_flip: bool = True
    # Strategy properties
    initial_capital: float = 10_000.0
    equity_pct_per_trade: float = 10.0
    commission_pct: float = 0.05       # percent per fill
    slippage_ticks: int = 1


# ════════════════════════════════════════════════════════════════════════════
# PINE-EXACT INDICATORS
# All take/return numpy float arrays; NaN plays the role of Pine's `na`.
# ════════════════════════════════════════════════════════════════════════════
def _sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


def _wma(x: np.ndarray, n: int) -> np.ndarray:
    w = np.arange(1, n + 1, dtype=float)
    wsum = w.sum()
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        win = x[i - n + 1 : i + 1]
        if not np.isnan(win).any():
            out[i] = float(np.dot(win, w) / wsum)
    return out


def _rma(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.rma: seeded with SMA of the first n values, then
    alpha = 1/n recursion."""
    out = np.full(len(x), np.nan)
    alpha = 1.0 / n
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


def _tr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        handle_na: bool) -> np.ndarray:
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    if handle_na:
        tr[0] = high[0] - low[0]
    else:
        tr[0] = np.nan
    return tr


def _rsi(x: np.ndarray, n: int) -> np.ndarray:
    diff = np.diff(x, prepend=np.nan)
    up = np.where(np.isnan(diff), np.nan, np.maximum(diff, 0.0))
    dn = np.where(np.isnan(diff), np.nan, np.maximum(-diff, 0.0))
    ru, rd = _rma(up, n), _rma(dn, n)
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


def _fixnan(x: np.ndarray) -> np.ndarray:
    return pd.Series(x).ffill().to_numpy()


def _dmi(high, low, close, n: int):
    up = np.diff(high, prepend=np.nan)
    down = -np.diff(low, prepend=np.nan)
    plus_dm = np.where(np.isnan(up), np.nan,
                       np.where((up > down) & (up > 0), up, 0.0))
    minus_dm = np.where(np.isnan(down), np.nan,
                        np.where((down > up) & (down > 0), down, 0.0))
    trur = _rma(_tr(high, low, close, handle_na=False), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = _fixnan(100.0 * _rma(plus_dm, n) / trur)
        minus = _fixnan(100.0 * _rma(minus_dm, n) / trur)
        s = plus + minus
        adx = 100.0 * _rma(np.abs(plus - minus) / np.where(s == 0, 1.0, s), n)
    return plus, minus, adx


def _linreg(x: np.ndarray, n: int) -> np.ndarray:
    """Least-squares fitted value at the newest bar (offset 0)."""
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


def _hma(x: np.ndarray, n: int) -> np.ndarray:
    # VERIFY: Pine built-in uses wma(2*wma(n/2) - wma(n), floor(sqrt(n)));
    # n/2 truncated -> 4 and floor(sqrt(9)) -> 3 for the default n = 9.
    half = max(1, n // 2)
    root = max(1, int(math.floor(math.sqrt(n))))
    return _wma(2.0 * _wma(x, half) - _wma(x, n), root)


def _tracker(high, low, close, n: int, mult: float):
    a = _wma(_tr(high, low, close, handle_na=True), n)
    up = high + a * mult
    dn = low - a * mult
    trk = np.full(len(close), np.nan)
    d = np.ones(len(close), dtype=int)
    t, direction = np.nan, 1
    for i in range(len(close)):
        if np.isnan(t):
            t = dn[i]                       # stays NaN until WMA is ready
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


def _gt(a, b) -> np.ndarray:
    """Pine comparison: anything involving na is false."""
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(a) | np.isnan(b), False, a > b)


def compute_indicators(df: pd.DataFrame, p: TASParams) -> pd.DataFrame:
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)

    trk, tdir = _tracker(h, l, c, p.trend_atr_len, p.trend_mult)
    eq = _hma(_linreg(c, p.eq_len), p.eq_smooth)
    eq_prev = np.roll(eq, 1)
    eq_prev[0] = np.nan
    rsi = _rsi(c, p.rsi_len)
    rsi_ma = _sma(rsi, p.rsi_smooth)
    dip, dim, adx = _dmi(h, l, c, p.dmi_len)

    def pm(cond):  # +1 / -1 component
        return np.where(cond, 1, -1)

    score = (tdir
             + pm(_gt(c, eq))
             + pm(_gt(eq, eq_prev))
             + pm(_gt(rsi, np.full(len(c), 50.0)))
             + pm(_gt(rsi, rsi_ma))
             + pm(_gt(dip, dim)))
    out = df.copy()
    out["trk"] = trk
    out["tdir"] = tdir
    out["eq"] = eq
    out["rsi"] = rsi
    out["rsi_ma"] = rsi_ma
    out["adx"] = adx
    out["score"] = score
    out["score_pct"] = score / 6.0 * 100.0
    out["r_atr"] = _rma(_tr(h, l, c, handle_na=True), p.risk_atr_len)
    return out


# ════════════════════════════════════════════════════════════════════════════
# BROKER EMULATOR + STRATEGY LOGIC
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class _Bracket:
    name: str            # "TP1" / "TP2" / "TP3"
    qty: float           # quantity reserved for this exit (set on fill)
    limit: float
    stop: float
    filled: bool = False


@dataclass
class _Position:
    side: int                    # +1 long, -1 short
    qty: float
    entry_price: float           # fill price
    entry_time: pd.Timestamp
    entry_bar: int
    entry_fee: float
    plan_entry: float            # signal-bar close (ePlan)
    init_stop: float
    brackets: list = field(default_factory=list)
    remaining: float = 0.0
    realized: float = 0.0        # net of fees, all partial fills
    fills: list = field(default_factory=list)


def _round_tick(x: float, tick: float) -> float:
    return round(x / tick) * tick


def run_backtest(df: pd.DataFrame, tick: float,
                 params: TASParams = TASParams(),
                 start: Optional[str] = None,
                 end: Optional[str] = None,
                 min_quote_volume_30d: Optional[float] = None,
                 tiered_slippage: bool = False) -> dict:
    """Run TAS on one symbol.

    df: columns open_time (UTC), open, high, low, close[, volume, vol_30d_med],
        sorted, no duplicates. Give it the FULL history: indicators warm
        up on everything, signals are only taken inside [start, end]
        (Pine "Limit Backtest Window").
    tick: exchange tick size for the symbol (required, no default).
    min_quote_volume_30d: if set, signals only taken when vol_30d_med >= this threshold.
    tiered_slippage: if True and vol_30d_med present, use volume-tiered slippage.
    """
    if tick is None or tick <= 0:
        raise ValueError("tick size is required")
    p = params
    d = compute_indicators(df.reset_index(drop=True), p)
    t_open = pd.to_datetime(d["open_time"], utc=True)
    O, H, L, C = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    trk, tdir = d["trk"].to_numpy(), d["tdir"].to_numpy()
    spct, ratr = d["score_pct"].to_numpy(), d["r_atr"].to_numpy()
    vol_arr = d["vol_30d_med"].to_numpy(float) if "vol_30d_med" in d.columns else None
    n = len(d)
    t_start = pd.Timestamp(start, tz="UTC") if start else None
    t_end = pd.Timestamp(end, tz="UTC") if end else None
    base_slip = p.slippage_ticks * tick
    fee_rate = p.commission_pct / 100.0

    def get_slip(bar: int, price: float) -> float:
        if tiered_slippage and vol_arr is not None:
            v = vol_arr[bar]
            if not np.isnan(v):
                if v >= 500_000_000:
                    return 1.0 * tick
                elif v >= 100_000_000:
                    return max(tick, _round_tick(0.0002 * price, tick))
                else:
                    return max(tick, _round_tick(0.0005 * price, tick))
        return base_slip

    # Pine `var` state
    trend_signaled, trend_start = False, 0
    plan_dir = 0
    e_plan = sl_lvl = tp1 = tp2 = tp3 = np.nan
    tp1_hit = tp2_hit = False

    realized_total = 0.0
    pos: Optional[_Position] = None
    pending_entry = None          # (side, qty, plan) for next open
    pending_close = False         # strategy.close at next open
    pending_window_close = False
    trade_rows, positions, equity_curve = [], [], []

    def close_qty(q: float, price: float, bar: int, reason: str):
        nonlocal pos, realized_total
        fee = price * q * fee_rate
        entry_fee_share = pos.entry_fee * q / pos.qty
        pnl = pos.side * (price - pos.entry_price) * q - fee - entry_fee_share
        pos.realized += pnl
        pos.remaining -= q
        realized_total += pnl
        trade_rows.append({
            "side": "long" if pos.side == 1 else "short",
            "entry_time": pos.entry_time, "entry_price": pos.entry_price,
            "exit_time": t_open[bar], "exit_price": price,
            "qty": q, "pnl": pnl, "exit_reason": reason,
        })
        if pos.remaining <= pos.qty * 1e-9:
            risk_unit = abs(pos.plan_entry - pos.init_stop) * pos.qty
            positions.append({
                "side": "long" if pos.side == 1 else "short",
                "entry_time": pos.entry_time, "entry_price": pos.entry_price,
                "exit_time": t_open[bar], "qty": pos.qty,
                "init_stop": pos.init_stop, "plan_entry": pos.plan_entry,
                "net_pnl": pos.realized,
                "r_multiple": pos.realized / risk_unit if risk_unit > 0 else np.nan,
                "last_exit_reason": reason,
            })
            pos = None

    def market_close_all(bar: int, reason: str):
        price = O[bar] - pos.side * get_slip(bar, O[bar])
        close_qty(pos.remaining, price, bar, reason)

    for i in range(n):
        # ── 1. BROKER: market orders at this bar's open ──────────────────
        if pos is not None and (pending_close or pending_window_close):
            market_close_all(i, "Window End" if pending_window_close else "Trend Flip")
        pending_close = pending_window_close = False

        if pending_entry is not None:
            side, qty, plan = pending_entry
            if pos is not None and pos.side != side:
                market_close_all(i, "Reverse")
            if pos is None:
                price = O[i] + side * get_slip(i, O[i])
                fee = price * qty * fee_rate
                pos = _Position(side=side, qty=qty, entry_price=price,
                                entry_time=t_open[i], entry_bar=i,
                                entry_fee=fee, plan_entry=plan["e"],
                                init_stop=plan["sl"], remaining=qty)
                q1 = qty * p.tp1_qty_pct / 100.0
                q2 = qty * p.tp2_qty_pct / 100.0
                pos.brackets = [
                    _Bracket("TP1", q1, plan["tp1"], plan["sl"]),
                    _Bracket("TP2", q2, plan["tp2"], plan["sl"]),
                    _Bracket("TP3", qty - q1 - q2, plan["tp3"], plan["sl"]),
                ]
            pending_entry = None

        # ── 2. BROKER: stop / limit exits along the intrabar path ────────
        if pos is not None:
            s = pos.side
            open_ = O[i]
            # gap fills at the open
            live = [b for b in pos.brackets if not b.filled]
            for b in live:
                if pos is None:
                    break
                if (s == 1 and open_ <= b.stop) or (s == -1 and open_ >= b.stop):
                    b.filled = True
                    q = pos.remaining if b.name == "TP3" else min(b.qty, pos.remaining)
                    close_qty(q, open_ - s * get_slip(i, open_), i, b.name + " SL")
                elif (s == 1 and open_ >= b.limit) or (s == -1 and open_ <= b.limit):
                    b.filled = True
                    q = pos.remaining if b.name == "TP3" else min(b.qty, pos.remaining)
                    close_qty(q, open_, i, b.name)
            if pos is not None:
                if abs(H[i] - open_) <= abs(open_ - L[i]):
                    path = [open_, H[i], L[i], C[i]]
                else:
                    path = [open_, L[i], H[i], C[i]]
                events = []   # (segment index, fraction, bracket, kind, level)
                for b in pos.brackets:
                    if b.filled:
                        continue
                    for kind, lvl in (("stop", b.stop), ("limit", b.limit)):
                        for seg in range(3):
                            a, z = path[seg], path[seg + 1]
                            lo, hi = min(a, z), max(a, z)
                            if lo <= lvl <= hi and a != z:
                                # direction check: stop is hit moving against,
                                # limit moving in favour
                                against = (z < a) if s == 1 else (z > a)
                                if (kind == "stop") == against:
                                    events.append((seg, abs(lvl - a) / abs(z - a), b, kind, lvl))
                                    break
                events.sort(key=lambda e: (e[0], e[1], e[2].name))
                for seg, frac, b, kind, lvl in events:
                    if pos is None:
                        break
                    if b.filled:
                        continue
                    b.filled = True
                    q = min(b.qty, pos.remaining) if b.name != "TP3" else pos.remaining
                    if kind == "stop":
                        close_qty(q, lvl - s * get_slip(i, lvl), i, b.name + " SL")
                    else:
                        close_qty(q, lvl, i, b.name)

        # ── 3. SCRIPT on bar close ───────────────────────────────────────
        bull_flip = i > 0 and tdir[i] == 1 and tdir[i - 1] == -1
        bear_flip = i > 0 and tdir[i] == -1 and tdir[i - 1] == 1
        if bull_flip or bear_flip:
            trend_signaled, trend_start = False, i

        ok_b = (not np.isnan(spct[i])) and spct[i] >= p.min_score_pct
        ok_s = (not np.isnan(spct[i])) and spct[i] <= -p.min_score_pct
        t_bar = t_open[i]
        in_date = (t_start is None or t_bar >= t_start) and (t_end is None or t_bar <= t_end)
        ok_vol = True
        if min_quote_volume_30d is not None and vol_arr is not None:
            ok_vol = (not np.isnan(vol_arr[i])) and vol_arr[i] >= min_quote_volume_30d
        in_window = i - trend_start <= p.late_max
        cand_b = tdir[i] == 1 and (bull_flip or (p.allow_late and not trend_signaled and in_window))
        cand_s = tdir[i] == -1 and (bear_flip or (p.allow_late and not trend_signaled and in_window))
        buy_sig = cand_b and ok_b and in_date and ok_vol and p.direction != "Short Only" and not trend_signaled
        sell_sig = cand_s and ok_s and in_date and ok_vol and p.direction != "Long Only" and not trend_signaled
        if buy_sig or sell_sig:
            trend_signaled = True

        in_long = pos is not None and pos.side == 1
        in_short = pos is not None and pos.side == -1
        flat = pos is None
        buy_entry = buy_sig and (flat or (in_short and p.allow_reverse))
        sell_entry = sell_sig and (flat or (in_long and p.allow_reverse))

        if buy_entry or sell_entry:
            side = 1 if buy_entry else -1
            st = trk[i]
            if np.isnan(st) or (side == 1 and st >= C[i]) or (side == -1 and st <= C[i]):
                st = C[i] - side * ratr[i] * p.sl_atr_mult       # Pine guard
            risk = abs(C[i] - st)
            plan_dir, e_plan, sl_lvl = side, C[i], st
            tp1 = C[i] + side * risk * p.tp1_r
            tp2 = C[i] + side * risk * p.tp2_r
            tp3 = C[i] + side * risk * p.tp3_r
            tp1_hit = tp2_hit = False
            # equity incl. open profit at this close (percent_of_equity sizing)
            open_pnl = 0.0 if pos is None else pos.side * (C[i] - pos.entry_price) * pos.remaining
            equity = p.initial_capital + realized_total + open_pnl
            qty = equity * p.equity_pct_per_trade / 100.0 / C[i]
            pending_entry = (side, qty, {"e": e_plan, "sl": sl_lvl,
                                         "tp1": tp1, "tp2": tp2, "tp3": tp3})

        # trade management (breakeven / flip exit)
        if pos is not None and ((pos.side == 1 and plan_dir == 1 and not buy_entry)
                                or (pos.side == -1 and plan_dir == -1 and not sell_entry)):
            dd = plan_dir
            if not tp1_hit and (H[i] >= tp1 if dd == 1 else L[i] <= tp1):
                tp1_hit = True
                if p.be_after_tp1:
                    sl_lvl = max(sl_lvl, e_plan) if dd == 1 else min(sl_lvl, e_plan)
            if not tp2_hit and (H[i] >= tp2 if dd == 1 else L[i] <= tp2):
                tp2_hit = True
            if p.exit_on_flip and (bear_flip if dd == 1 else bull_flip):
                pending_close = True
            # refresh protective stops (strategy.exit re-issued every bar)
            for b in pos.brackets:
                b.stop = sl_lvl

        if t_end is not None and t_bar > t_end and pos is not None:
            pending_window_close = True

        # mark-to-market equity at close
        open_pnl = 0.0 if pos is None else pos.side * (C[i] - pos.entry_price) * pos.remaining
        equity_curve.append(p.initial_capital + realized_total + open_pnl)

    trades = pd.DataFrame(trade_rows)
    pos_df = pd.DataFrame(positions)
    eq = pd.Series(equity_curve, index=t_open)
    return {"trades": trades, "positions": pos_df, "equity": eq,
            "stats": summarize(trades, pos_df, eq, p.initial_capital)}


# ════════════════════════════════════════════════════════════════════════════
# STATS
# ════════════════════════════════════════════════════════════════════════════
def summarize(trades: pd.DataFrame, positions: pd.DataFrame,
              equity: pd.Series, initial_capital: float) -> dict:
    if trades.empty:
        return {"closed_trades": 0, "positions": 0}
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    gp, gl = wins["pnl"].sum(), -losses["pnl"].sum()
    peak = equity.cummax()
    dd = (peak - equity)
    stats = {
        # TradingView-style (each exit fill = one closed trade)
        "closed_trades": int(len(trades)),
        "win_rate_pct": round(100 * len(wins) / len(trades), 2),
        "net_profit": round(trades["pnl"].sum(), 2),
        "net_profit_pct": round(100 * trades["pnl"].sum() / initial_capital, 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else np.inf,
        "max_drawdown": round(float(dd.max()), 2),           # close-to-close
        "max_drawdown_pct": round(float((dd / peak).max() * 100), 2),
        # Position-level (one row per complete position) - what Deskmind gates use
        "positions": int(len(positions)),
    }
    if not positions.empty:
        r = positions["r_multiple"].dropna()
        stats.update({
            "position_win_rate_pct": round(100 * (positions["net_pnl"] > 0).mean(), 2),
            "mean_r": round(float(r.mean()), 4),
            "median_r": round(float(r.median()), 4),
        })
    return stats


# ════════════════════════════════════════════════════════════════════════════
# DATA HELPERS
# ════════════════════════════════════════════════════════════════════════════
def resample(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 1h candles to '4h' / '1D' (UTC-aligned, like TradingView
    for Binance perpetuals). Incomplete last bucket is dropped."""
    x = df_1h.copy()
    x["open_time"] = pd.to_datetime(x["open_time"], utc=True)
    x = x.set_index("open_time")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in x:
        agg["volume"] = "sum"
    out = x.resample(rule, label="left", closed="left").agg(agg)
    counts = x["close"].resample(rule, label="left", closed="left").count()
    expected = int(pd.Timedelta(rule) / pd.Timedelta("1h"))
    out = out[counts == expected].dropna(subset=["open"])
    return out.reset_index()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run TAS on one OHLCV file")
    ap.add_argument("file", help="CSV or Parquet with open_time,open,high,low,close")
    ap.add_argument("--tick", type=float, required=True)
    ap.add_argument("--resample", default=None, help="e.g. 4h or 1D (input must be 1h)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    data = pd.read_parquet(a.file) if a.file.endswith(".parquet") else pd.read_csv(a.file)
    if a.resample:
        data = resample(data, a.resample)
    res = run_backtest(data, tick=a.tick, start=a.start, end=a.end)
    for k, v in res["stats"].items():
        print(f"{k:24s} {v}")
