"""TAS (Trend Analysis Strategy) v1.0.0 ported to Strategy Lab format."""

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import ExitBracket, TradingViewBroker
from deskmind.strategy_lab.indicators import atr, dmi, gt, hma, linreg, rsi, sma, tracker


class Strategy:
    """Trend Analysis Strategy (Pine Script v6 port)."""

    name: str = "TAS"
    version: str = "1.0.0"
    timeframe: str = "4h"

    params: dict[str, Any] = {
        "trend_atr_len": 14,
        "trend_mult": 2.2,
        "rsi_len": 14,
        "rsi_smooth": 14,
        "dmi_len": 14,
        "eq_len": 55,
        "eq_smooth": 9,
        "min_score_pct": 50.0,
        "allow_reverse": True,
        "allow_late": True,
        "late_max": 10,
        "direction": "Both",  # "Both" | "Long Only" | "Short Only"
        "risk_atr_len": 14,
        "sl_atr_mult": 2.0,  # guard multiplier
        "be_after_tp1": True,
        "tp1_r": 1.5,
        "tp1_qty_pct": 33.0,
        "tp2_r": 2.5,
        "tp2_qty_pct": 33.0,
        "tp3_r": 3.5,
        "exit_on_flip": True,
        "initial_capital": 10_000.0,
        "equity_pct_per_trade": 10.0,
        "commission_pct": 0.05,
        "slippage_ticks": 1,
    }

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}
        self.trend_signaled = False
        self.trend_start = 0
        self.plan_dir = 0
        self.e_plan = float("nan")
        self.sl_lvl = float("nan")
        self.tp1 = float("nan")
        self.tp2 = float("nan")
        self.tp3 = float("nan")
        self.tp1_hit = False
        self.tp2_hit = False

    def reset_state(self) -> None:
        self.trend_signaled = False
        self.trend_start = 0
        self.plan_dir = 0
        self.e_plan = float("nan")
        self.sl_lvl = float("nan")
        self.tp1 = float("nan")
        self.tp2 = float("nan")
        self.tp3 = float("nan")
        self.tp1_hit = False
        self.tp2_hit = False

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all TAS indicators vectorially."""
        p = self.params
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)

        trk, tdir = tracker(h, l, c, p["trend_atr_len"], p["trend_mult"])
        eq = hma(linreg(c, p["eq_len"]), p["eq_smooth"])
        eq_prev = np.roll(eq, 1)
        eq_prev[0] = np.nan
        rsi_val = rsi(c, p["rsi_len"])
        rsi_ma = sma(rsi_val, p["rsi_smooth"])
        dip, dim, adx = dmi(h, l, c, p["dmi_len"])

        def pm(cond: np.ndarray) -> np.ndarray:
            return np.where(cond, 1, -1)

        score = (
            tdir
            + pm(gt(c, eq))
            + pm(gt(eq, eq_prev))
            + pm(gt(rsi_val, np.full(len(c), 50.0)))
            + pm(gt(rsi_val, rsi_ma))
            + pm(gt(dip, dim))
        )
        out = df.copy()
        out["trk"] = trk
        out["tdir"] = tdir
        out["eq"] = eq
        out["rsi"] = rsi_val
        out["rsi_ma"] = rsi_ma
        out["adx"] = adx
        out["score"] = score
        out["score_pct"] = score / 6.0 * 100.0
        out["r_atr"] = atr(h, l, c, p["risk_atr_len"])
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        """Execute TAS state machine and trading rules on bar close."""
        if i == 0:
            self.reset_state()

        p = self.params
        d = ctx.df
        tdir = d["tdir"].to_numpy()
        spct = d["score_pct"].to_numpy()
        trk = d["trk"].to_numpy()
        ratr = d["r_atr"].to_numpy()

        bull_flip = i > 0 and tdir[i] == 1 and tdir[i - 1] == -1
        bear_flip = i > 0 and tdir[i] == -1 and tdir[i - 1] == 1
        if bull_flip or bear_flip:
            self.trend_signaled = False
            self.trend_start = i

        ok_b = (not np.isnan(spct[i])) and spct[i] >= p["min_score_pct"]
        ok_s = (not np.isnan(spct[i])) and spct[i] <= -p["min_score_pct"]
        in_date = ctx.in_date

        ok_vol = True
        if broker.min_quote_volume_30d is not None and ctx.vol_30d_med is not None:
            ok_vol = (not np.isnan(ctx.vol_30d_med)) and (
                ctx.vol_30d_med >= broker.min_quote_volume_30d
            )

        in_window = (i - self.trend_start) <= p["late_max"]
        cand_b = tdir[i] == 1 and (
            bull_flip or (p["allow_late"] and not self.trend_signaled and in_window)
        )
        cand_s = tdir[i] == -1 and (
            bear_flip or (p["allow_late"] and not self.trend_signaled and in_window)
        )

        buy_sig = (
            cand_b
            and ok_b
            and in_date
            and ok_vol
            and p["direction"] != "Short Only"
            and not self.trend_signaled
        )
        sell_sig = (
            cand_s
            and ok_s
            and in_date
            and ok_vol
            and p["direction"] != "Long Only"
            and not self.trend_signaled
        )

        if buy_sig or sell_sig:
            self.trend_signaled = True

        in_long = broker.is_long
        in_short = broker.is_short
        flat = broker.is_flat
        buy_entry = buy_sig and (flat or (in_short and p["allow_reverse"]))
        sell_entry = sell_sig and (flat or (in_long and p["allow_reverse"]))

        if buy_entry or sell_entry:
            side = 1 if buy_entry else -1
            st = trk[i]
            if np.isnan(st) or (side == 1 and st >= ctx.close) or (side == -1 and st <= ctx.close):
                st = ctx.close - side * ratr[i] * p["sl_atr_mult"]

            risk = abs(ctx.close - st)
            self.plan_dir = side
            self.e_plan = ctx.close
            self.sl_lvl = st
            self.tp1 = ctx.close + side * risk * p["tp1_r"]
            self.tp2 = ctx.close + side * risk * p["tp2_r"]
            self.tp3 = ctx.close + side * risk * p["tp3_r"]
            self.tp1_hit = False
            self.tp2_hit = False

            brackets = [
                ExitBracket("TP1", p["tp1_qty_pct"], self.tp1, self.sl_lvl),
                ExitBracket("TP2", p["tp2_qty_pct"], self.tp2, self.sl_lvl),
                ExitBracket("TP3", 34.0, self.tp3, self.sl_lvl),
            ]
            broker.entry(
                side=side,
                brackets=brackets,
                plan_entry=self.e_plan,
                init_stop=self.sl_lvl,
                current_close=ctx.close,
            )

        # Trade management (breakeven / flip exit)
        if broker.position is not None and (
            (broker.position.side == 1 and self.plan_dir == 1 and not buy_entry)
            or (broker.position.side == -1 and self.plan_dir == -1 and not sell_entry)
        ):
            dd = self.plan_dir
            if not self.tp1_hit and (ctx.high >= self.tp1 if dd == 1 else ctx.low <= self.tp1):
                self.tp1_hit = True
                if p["be_after_tp1"]:
                    self.sl_lvl = (
                        max(self.sl_lvl, self.e_plan)
                        if dd == 1
                        else min(self.sl_lvl, self.e_plan)
                    )
            if not self.tp2_hit and (ctx.high >= self.tp2 if dd == 1 else ctx.low <= self.tp2):
                self.tp2_hit = True
            if p["exit_on_flip"] and (bear_flip if dd == 1 else bull_flip):
                broker.close(reason="Trend Flip")

            broker.update_brackets(stop=self.sl_lvl)
