"""TradingView broker emulator for Strategy Lab.

Replicates TradingView Pine Script strategy execution rules:
  - Intrabar stop / limit exits along the TradingView price path
  - Next-bar open market execution for entry/close orders
  - Gap fills through stops/limits at open
  - Multiple exit brackets (TP1 / TP2 / TP3 with percentage sizing)
  - Stop update / breakeven support
  - Commission rate (percentage per fill)
  - Slippage (ticks or volume-tiered)
  - Percentage of equity sizing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class ExitBracket:
    name: str  # e.g. "TP1", "TP2", "TP3"
    qty_pct: float  # percentage of position allocated to this exit (e.g. 33.0)
    limit: float  # take profit target price
    stop: float  # protective stop loss price


@dataclass
class _ActiveBracket:
    name: str
    qty: float
    limit: float
    stop: float
    filled: bool = False


@dataclass
class Position:
    side: int  # +1 long, -1 short
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_bar: int
    entry_fee: float
    plan_entry: float
    init_stop: float
    brackets: list[_ActiveBracket] = field(default_factory=list)
    remaining: float = 0.0
    realized: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)


def round_tick(price: float, tick: float) -> float:
    """Round price to the nearest valid exchange tick."""
    return round(price / tick) * tick


class TradingViewBroker:
    """Broker emulator matching TradingView's execution engine."""

    def __init__(
        self,
        tick: float,
        initial_capital: float = 10_000.0,
        equity_pct_per_trade: float = 10.0,
        commission_pct: float = 0.05,
        slippage_ticks: int = 1,
        allow_reverse: bool = True,
        min_quote_volume_30d: float | None = None,
        tiered_slippage: bool = False,
    ):
        if tick is None or tick <= 0:
            raise ValueError("Valid positive tick size is required")
        self.tick = tick
        self.initial_capital = initial_capital
        self.equity_pct_per_trade = equity_pct_per_trade
        self.commission_pct = commission_pct
        self.fee_rate = commission_pct / 100.0
        self.slippage_ticks = slippage_ticks
        self.base_slip = slippage_ticks * tick
        self.allow_reverse = allow_reverse
        self.min_quote_volume_30d = min_quote_volume_30d
        self.tiered_slippage = tiered_slippage

        # State
        self.pos: Position | None = None
        self.realized_total = 0.0
        self.pending_entry: tuple[int, float, dict[str, Any]] | None = None
        self.pending_close = False
        self.pending_window_close = False
        self.close_reason = "Close"

        self.trade_rows: list[dict[str, Any]] = []
        self.position_rows: list[dict[str, Any]] = []
        self.equity_curve: list[float] = []

    # ---------------------------------------------------------------- Properties
    @property
    def position(self) -> Position | None:
        return self.pos

    @property
    def is_long(self) -> bool:
        return self.pos is not None and self.pos.side == 1

    @property
    def is_short(self) -> bool:
        return self.pos is not None and self.pos.side == -1

    @property
    def is_flat(self) -> bool:
        return self.pos is None

    # ---------------------------------------------------------------- Strategy APIs
    def entry(
        self,
        side: int | str,
        brackets: list[ExitBracket] | None = None,
        plan_entry: float | None = None,
        init_stop: float | None = None,
        current_close: float | None = None,
        qty: float | None = None,
    ) -> None:
        """Schedule a market order entry for next bar's open."""
        side_val = 1 if side in (1, "long", "Long") else -1
        if qty is None and current_close is not None and current_close > 0:
            open_pnl = (
                0.0
                if self.pos is None
                else self.pos.side * (current_close - self.pos.entry_price) * self.pos.remaining
            )
            equity = self.initial_capital + self.realized_total + open_pnl
            qty = equity * self.equity_pct_per_trade / 100.0 / current_close

        plan = {
            "brackets": brackets or [],
            "plan_entry": plan_entry,
            "init_stop": init_stop,
            "current_close": current_close,
            "fixed_qty": qty,
        }
        self.pending_entry = (side_val, qty if qty is not None else 0.0, plan)

    def close(self, reason: str = "Strategy Close") -> None:
        """Schedule closing the open position at next bar's open."""
        self.pending_close = True
        self.close_reason = reason

    def update_brackets(
        self,
        stop: float | None = None,
        limit: float | None = None,
        bracket_name: str | None = None,
    ) -> None:
        """Update protective stop or target limit on live brackets (e.g. breakeven)."""
        if self.pos is None:
            return
        for b in self.pos.brackets:
            if bracket_name is None or b.name == bracket_name:
                if stop is not None:
                    b.stop = stop
                if limit is not None:
                    b.limit = limit

    # ---------------------------------------------------------------- Internal Engine
    def get_slip(self, bar: int, price: float, vol_arr: np.ndarray | None) -> float:
        if self.tiered_slippage and vol_arr is not None:
            v = vol_arr[bar]
            if not np.isnan(v):
                if v >= 500_000_000:
                    return 1.0 * self.tick
                elif v >= 100_000_000:
                    return max(self.tick, round_tick(0.0002 * price, self.tick))
                else:
                    return max(self.tick, round_tick(0.0005 * price, self.tick))
        return self.base_slip

    def _close_qty(
        self,
        q: float,
        price: float,
        bar: int,
        reason: str,
        t_open: pd.Series,
    ) -> None:
        pos = self.pos
        if pos is None:
            return
        fee = price * q * self.fee_rate
        entry_fee_share = pos.entry_fee * q / pos.qty
        pnl = pos.side * (price - pos.entry_price) * q - fee - entry_fee_share
        pos.realized += pnl
        pos.remaining -= q
        self.realized_total += pnl

        self.trade_rows.append(
            {
                "side": "long" if pos.side == 1 else "short",
                "entry_time": pos.entry_time,
                "entry_price": pos.entry_price,
                "exit_time": t_open[bar],
                "exit_price": price,
                "qty": q,
                "pnl": pnl,
                "exit_reason": reason,
            }
        )

        if pos.remaining <= pos.qty * 1e-9:
            risk_unit = abs(pos.plan_entry - pos.init_stop) * pos.qty
            self.position_rows.append(
                {
                    "side": "long" if pos.side == 1 else "short",
                    "entry_time": pos.entry_time,
                    "entry_price": pos.entry_price,
                    "exit_time": t_open[bar],
                    "qty": pos.qty,
                    "init_stop": pos.init_stop,
                    "plan_entry": pos.plan_entry,
                    "net_pnl": pos.realized,
                    "r_multiple": pos.realized / risk_unit if risk_unit > 0 else np.nan,
                    "last_exit_reason": reason,
                }
            )
            self.pos = None

    def _market_close_all(
        self,
        bar: int,
        reason: str,
        open_arr: np.ndarray,
        t_open: pd.Series,
        vol_arr: np.ndarray | None,
    ) -> None:
        if self.pos is None:
            return
        slip = self.get_slip(bar, open_arr[bar], vol_arr)
        price = open_arr[bar] - self.pos.side * slip
        self._close_qty(self.pos.remaining, price, bar, reason, t_open)

    def execute_bar_open(
        self,
        i: int,
        open_arr: np.ndarray,
        close_arr: np.ndarray,
        t_open: pd.Series,
        vol_arr: np.ndarray | None,
    ) -> None:
        """Execute market orders scheduled for bar i's open."""
        # 1. Process pending close
        if self.pos is not None and (self.pending_close or self.pending_window_close):
            reason = "Window End" if self.pending_window_close else self.close_reason
            self._market_close_all(i, reason, open_arr, t_open, vol_arr)
        self.pending_close = False
        self.pending_window_close = False

        # 2. Process pending entry
        if self.pending_entry is not None:
            side, fixed_qty, plan = self.pending_entry
            # If in opposite position, reverse close first
            if self.pos is not None and self.pos.side != side and self.allow_reverse:
                self._market_close_all(i, "Reverse", open_arr, t_open, vol_arr)

            if self.pos is None:
                slip = self.get_slip(i, open_arr[i], vol_arr)
                price = open_arr[i] + side * slip
                ref_close = plan.get("current_close", close_arr[i - 1] if i > 0 else open_arr[i])
                if fixed_qty and fixed_qty > 0:
                    qty = fixed_qty
                else:
                    equity = self.initial_capital + self.realized_total
                    qty = equity * self.equity_pct_per_trade / 100.0 / ref_close

                fee = price * qty * self.fee_rate
                e_plan = plan.get("plan_entry", ref_close)
                init_stop = plan.get("init_stop", e_plan - side * self.base_slip)

                self.pos = Position(
                    side=side,
                    qty=qty,
                    entry_price=price,
                    entry_time=t_open[i],
                    entry_bar=i,
                    entry_fee=fee,
                    plan_entry=e_plan,
                    init_stop=init_stop,
                    remaining=qty,
                )

                # Initialize active brackets
                raw_brackets: list[ExitBracket] = plan.get("brackets", [])
                active_brackets: list[_ActiveBracket] = []
                remaining_qty = qty
                for b in raw_brackets:
                    if b.name == "TP3" or b.qty_pct >= 99.0:
                        b_qty = remaining_qty
                    else:
                        b_qty = qty * b.qty_pct / 100.0
                    remaining_qty -= b_qty
                    active_brackets.append(
                        _ActiveBracket(name=b.name, qty=b_qty, limit=b.limit, stop=b.stop)
                    )
                self.pos.brackets = active_brackets

            self.pending_entry = None

    def execute_bar_intrabar(
        self,
        i: int,
        open_arr: np.ndarray,
        high_arr: np.ndarray,
        low_arr: np.ndarray,
        close_arr: np.ndarray,
        t_open: pd.Series,
        vol_arr: np.ndarray | None,
    ) -> None:
        """Check protective stops and take profit targets along the intrabar price path."""
        pos = self.pos
        if pos is None:
            return

        s = pos.side
        open_ = open_arr[i]

        # 1. Gap fills at the open
        live = [b for b in pos.brackets if not b.filled]
        for b in live:
            if self.pos is None:
                break
            if (s == 1 and open_ <= b.stop) or (s == -1 and open_ >= b.stop):
                b.filled = True
                q = self.pos.remaining if b.name == "TP3" else min(b.qty, self.pos.remaining)
                slip = self.get_slip(i, open_, vol_arr)
                self._close_qty(q, open_ - s * slip, i, b.name + " SL", t_open)
            elif (s == 1 and open_ >= b.limit) or (s == -1 and open_ <= b.limit):
                b.filled = True
                q = self.pos.remaining if b.name == "TP3" else min(b.qty, self.pos.remaining)
                self._close_qty(q, open_, i, b.name, t_open)

        # 2. Intrabar price path
        if self.pos is not None:
            if abs(high_arr[i] - open_) <= abs(open_ - low_arr[i]):
                path = [open_, high_arr[i], low_arr[i], close_arr[i]]
            else:
                path = [open_, low_arr[i], high_arr[i], close_arr[i]]

            events = []
            for b in self.pos.brackets:
                if b.filled:
                    continue
                for kind, lvl in (("stop", b.stop), ("limit", b.limit)):
                    for seg in range(3):
                        a, z = path[seg], path[seg + 1]
                        lo, hi = min(a, z), max(a, z)
                        if lo <= lvl <= hi and a != z:
                            against = (z < a) if s == 1 else (z > a)
                            if (kind == "stop") == against:
                                events.append((seg, abs(lvl - a) / abs(z - a), b, kind, lvl))
                                break

            events.sort(key=lambda e: (e[0], e[1], e[2].name))
            for _seg, _frac, b, kind, lvl in events:
                if self.pos is None:
                    break
                if b.filled:
                    continue
                b.filled = True
                q = min(b.qty, self.pos.remaining) if b.name != "TP3" else self.pos.remaining
                if kind == "stop":
                    slip = self.get_slip(i, lvl, vol_arr)
                    self._close_qty(q, lvl - s * slip, i, b.name + " SL", t_open)
                else:
                    self._close_qty(q, lvl, i, b.name, t_open)

    def record_bar_close(
        self,
        i: int,
        close_arr: np.ndarray,
        t_bar: pd.Timestamp,
        t_end: pd.Timestamp | None,
    ) -> None:
        """Mark-to-market equity at bar close and handle window close flags."""
        if t_end is not None and t_bar > t_end and self.pos is not None:
            self.pending_window_close = True

        open_pnl = (
            0.0
            if self.pos is None
            else self.pos.side * (close_arr[i] - self.pos.entry_price) * self.pos.remaining
        )
        self.equity_curve.append(self.initial_capital + self.realized_total + open_pnl)

    def get_results(self, t_open: pd.Series) -> dict[str, Any]:
        """Compile and summarize trades, positions, equity curve, and performance stats."""
        trades = pd.DataFrame(self.trade_rows)
        positions = pd.DataFrame(self.position_rows)
        eq = pd.Series(self.equity_curve, index=t_open[: len(self.equity_curve)])
        stats = self._summarize(trades, positions, eq)
        return {
            "trades": trades,
            "positions": positions,
            "equity": eq,
            "stats": stats,
        }

    def _summarize(
        self,
        trades: pd.DataFrame,
        positions: pd.DataFrame,
        equity: pd.Series,
    ) -> dict[str, Any]:
        if trades.empty:
            return {"closed_trades": 0, "positions": 0}
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        gp, gl = wins["pnl"].sum(), -losses["pnl"].sum()
        peak = equity.cummax()
        dd = peak - equity
        stats = {
            "closed_trades": len(trades),
            "win_rate_pct": round(100.0 * len(wins) / len(trades), 2),
            "net_profit": round(float(trades["pnl"].sum()), 2),
            "net_profit_pct": round(100.0 * float(trades["pnl"].sum()) / self.initial_capital, 2),
            "gross_profit": round(float(gp), 2),
            "gross_loss": round(float(gl), 2),
            "profit_factor": (
                round(float(gp / gl), 3) if gl > 0 else (999.0 if gp > 0 else 0.0)
            ),
            "max_drawdown": round(float(dd.max()), 2),
            "max_drawdown_pct": round(float((dd / peak).max() * 100.0), 2),
            "positions": len(positions),
        }
        if not positions.empty:
            r = positions["r_multiple"].dropna()
            stats.update(
                {
                    "position_win_rate_pct": round(
                        100.0 * float((positions["net_pnl"] > 0).mean()), 2
                    ),
                    "mean_r": round(float(r.mean()), 4) if len(r) > 0 else 0.0,
                    "median_r": round(float(r.median()), 4) if len(r) > 0 else 0.0,
                }
            )
        return stats
