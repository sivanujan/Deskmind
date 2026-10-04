# Strategy Lab — Strategy Template & Conversion Guide

This guide defines the standardized format for writing and uploading standalone Python strategies to **Strategy Lab**.

Strategies are authored outside the repository (e.g. converted from TradingView Pine Script) and uploaded as **a single Python file**. Strategy Lab executes, checks, and backtests all strategies uniformly.

---

## 1. Safety Sandbox & Import Rules

To ensure strategies are pure, reproducible, and secure, each file undergoes an AST inspection prior to loading.

### Allowed Imports
Strategies may **only** import from:
- `numpy` (or `import numpy as np`)
- `pandas` (or `import pandas as pd`)
- `math`
- `deskmind.strategy_lab` (or `from deskmind.strategy_lab.indicators import ...`, `from deskmind.strategy_lab.broker import ExitBracket, TradingViewBroker`, etc.)
- Python typing & dataclasses (`typing`, `dataclasses`, `__future__`)

**Strictly Forbidden**:
- Filesystem access (`open`, `pathlib`, `os`, `sys`, `shutil`)
- Network access (`socket`, `urllib`, `requests`, `aiohttp`, `httpx`)
- Any imports outside the whitelist will cause the strategy to be **REJECTED** immediately.

---

## 2. Standard Strategy Protocol

Every strategy file must define a class named `Strategy` conforming to the following structure:

```python
from __future__ import annotations
import numpy as np
import pandas as pd
from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import ExitBracket, TradingViewBroker
from deskmind.strategy_lab.indicators import atr, rma, rsi, sma

class Strategy:
    # ── Required Metadata ────────────────────────────────────────────────────
    name: str = "StrategyName"
    version: str = "1.0.0"
    timeframe: str = "4h"  # e.g. "15m", "1h", "4h", "1d"
    params: dict = {
        # Configurable parameters
    }

    def __init__(self) -> None:
        self.reset_state()

    def reset_state(self) -> None:
        """Reset stateful tracking variables (e.g. signal states, flips)."""
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Vectorized indicator computation across the full history.

        Rules:
        - Must be purely vectorized or strictly causal (no future data).
        - df columns: open, high, low, close, volume, open_time.
        - Returns a DataFrame containing all computed series.
        """
        out = df.copy()
        # compute series
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        """Called at the close of bar i (same as Pine Script on bar close).

        Parameters:
        - i: current bar index
        - ctx: BarContext containing:
            ctx.open, ctx.high, ctx.low, ctx.close, ctx.volume
            ctx.t_bar (timestamp), ctx.in_date (within backtest date window)
            ctx.tick (symbol tick size), ctx.df (indicators dataframe)
            ctx.vol_30d_med (optional 30-day quote volume filter)
        - broker: TradingViewBroker instance for submitting orders & brackets.
        """
        pass
```

---

## 3. TradingView Execution Model

The `TradingViewBroker` strictly replicates TradingView's execution order and mechanics:

1. **Bar Open ($i+1$)**:
   - Market orders scheduled at bar $i$ close fill at `open[i+1] + side * slippage`.
   - Reverse orders close opposite positions first at market open, then open the new direction.
   - Gap fills: If market opens through a stop or limit level, the fill occurs at the opening price.

2. **Intrabar Path Rule ($i+1$)**:
   - Evaluates intra-candle price path:
     - If $|high - open| \le |open - low|$, path is `open → high → low → close`.
     - Otherwise, path is `open → low → high → close`.
   - Traverses path segments to determine whether stops or limits hit first.
   - Stop slippage: Stop executions incur market slippage (`price - side * slippage`). Limits fill exactly at the limit price.

3. **Bar Close ($i$)**:
   - `on_bar(i, ctx, broker)` executes.
   - Sizing (`% of equity`): Automatically sizes positions using current mark-to-market equity at bar close.
   - Signals placed here become pending market orders for the next bar's open.

### Broker Order API
- `broker.entry(side=1 | -1, brackets=[...], plan_entry=..., init_stop=..., current_close=ctx.close)`:
  Schedules an entry at next open.
- `broker.close(reason="Reason")`:
  Closes the position at next open.
- `broker.update_brackets(stop=..., limit=...)`:
  Updates live protective stops (e.g. breakeven logic).
- `ExitBracket(name="TP1", qty_pct=33.0, limit=price, stop=stop_price)`:
  Defines multi-target bracket orders.

---

## 4. Automatic Acceptance Checks

Before any strategy is accepted into the lab, `checks.py` runs four automated gates:
1. **Execution**: Runs without exception on `ETHUSDT` for its declared timeframe.
2. **Accounting**: Validates that $\sum \text{Trade PnL} == \sum \text{Position PnL}$.
3. **Determinism**: Validates that identical backtests produce identical trades bit-for-bit.
4. **No-Look-Ahead**: Truncates data at $\frac{2}{3}$. Indicator values and closed positions before the truncation point must match the full run identically.

Any failure **REJECTS** the strategy with a descriptive error.

---

## 5. Reference Implementation: `tas_v1.py`

Below is the complete reference implementation converted from the TAS Pine Script:

```python
from __future__ import annotations

import numpy as np
import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import ExitBracket, TradingViewBroker
from deskmind.strategy_lab.indicators import atr, dmi, hma, linreg, rsi, tracker


class Strategy:
    """Trend + Momentum Scoring Strategy (TAS v1) converted from TradingView Pine Script."""

    name: str = "TAS"
    version: str = "1.0.0"
    timeframe: str = "4h"

    params: dict = {
        "tracker_len": 55,
        "fast_len": 9,
        "slow_len": 21,
        "rsi_len": 14,
        "rsi_ma_len": 14,
        "adx_len": 14,
        "adx_threshold": 20.0,
        "risk_atr_len": 14,
        "sl_atr_mult": 2.0,
        "tp1_r": 1.0,
        "tp2_r": 2.0,
        "tp3_r": 3.0,
        "tp1_qty_pct": 33.0,
        "tp2_qty_pct": 33.0,
        "be_after_tp1": True,
        "exit_on_flip": True,
        "allow_reverse": True,
        "min_score_pct": 50.0,
        "allow_late": True,
        "late_max": 2,
        "direction": "Both",
        "equity_pct_per_trade": 10.0,
    }

    def __init__(self) -> None:
        self.reset_state()

    def reset_state(self) -> None:
        self.trend_start = 0
        self.trend_signaled = False
        self.plan_dir = 0
        self.e_plan = 0.0
        self.sl_lvl = 0.0
        self.tp1 = 0.0
        self.tp2 = 0.0
        self.tp3 = 0.0
        self.tp1_hit = False
        self.tp2_hit = False

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)

        trk, tdir = tracker(h, l, c, p["tracker_len"])
        fast = linreg(c, p["fast_len"])
        slow = hma(c, p["slow_len"])
        eq = np.where(np.isnan(fast) | np.isnan(slow), np.nan, fast - slow)

        rsi_val, rsi_ma = rsi(c, p["rsi_len"], p["rsi_ma_len"])
        _, _, adx = dmi(h, l, c, p["adx_len"])

        s1 = np.where(np.isnan(tdir), 0, tdir)
        s2 = np.where(np.isnan(eq), 0, np.where(eq > 0, 1, np.where(eq < 0, -1, 0)))
        s3 = np.where(np.isnan(rsi_val), 0, np.where(rsi_val > 50, 1, np.where(rsi_val < 50, -1, 0)))
        s4 = np.where(
            np.isnan(rsi_val) | np.isnan(rsi_ma),
            0,
            np.where(rsi_val > rsi_ma, 1, np.where(rsi_val < rsi_ma, -1, 0)),
        )
        s5 = np.where(c > trk, 1, np.where(c < trk, -1, 0))
        s6 = np.where(adx >= p["adx_threshold"], 1, 0) * s1
        score = s1 + s2 + s3 + s4 + s5 + s6

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
                        max(self.sl_lvl, self.e_plan) if dd == 1 else min(self.sl_lvl, self.e_plan)
                    )
            if not self.tp2_hit and (ctx.high >= self.tp2 if dd == 1 else ctx.low <= self.tp2):
                self.tp2_hit = True
            if p["exit_on_flip"] and (bear_flip if dd == 1 else bull_flip):
                broker.close(reason="Trend Flip")

            # Update live protective stops
            broker.update_brackets(stop=self.sl_lvl)
```
