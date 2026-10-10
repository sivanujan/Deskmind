"""Breakout v1.0.0 (ported from the backtesting-trading-strategies skill).

Long when the close breaks above the highest high of the previous `lookback` bars (plus
`threshold` %); exit when it breaks below the lowest low of the previous `lookback` bars.
Long only. Fills at the next bar's open; protective stop 2 x ATR.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr, gt, rolling_max, rolling_min
from deskmind.strategy_lab.signal_rules import COMMON_PARAMS, act_on_signals


class Strategy:
    """Price range breakout (long only)."""

    name: str = "Breakout"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"lookback": 20, "threshold": 0.0, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        h, low_ = df["high"].to_numpy(float), df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        n, thr = int(p["lookback"]), float(p["threshold"]) / 100.0
        # Range of the PREVIOUS n bars (the current bar is not part of its own range).
        resistance = pd.Series(rolling_max(h, n)).shift(1).to_numpy() * (1 + thr)
        support = pd.Series(rolling_min(low_, n)).shift(1).to_numpy() * (1 - thr)
        out = df.copy()
        out["resistance"], out["support"] = resistance, support
        out["long_entry"] = gt(c, resistance)
        out["long_exit"] = gt(support, c)
        out["short_entry"] = False
        out["short_exit"] = False
        out["atr"] = atr(h, low_, c, p["risk_atr_len"])
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
