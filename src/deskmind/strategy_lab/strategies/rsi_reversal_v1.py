"""RSI Reversal v1.0.0 (ported from the backtesting-trading-strategies skill).

Long when RSI crosses back above the oversold level; short when it crosses back below the
overbought level. Each signal reverses an opposite position. Fills at the next bar's open;
protective stop 2 x ATR. RSI is Pine's (Wilder) RSI.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr, rsi
from deskmind.strategy_lab.signal_rules import COMMON_PARAMS, act_on_signals, crossover, crossunder


class Strategy:
    """RSI overbought/oversold reversal (long and short)."""

    name: str = "RSI_Reversal"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"period": 14, "overbought": 70.0, "oversold": 30.0, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        r = rsi(c, p["period"])
        oversold = np.full(len(c), float(p["oversold"]))
        overbought = np.full(len(c), float(p["overbought"]))
        out = df.copy()
        out["rsi"] = r
        out["long_entry"] = crossover(r, oversold)
        out["short_entry"] = crossunder(r, overbought)
        out["long_exit"] = False  # a long ends on the opposite (short) signal or its stop
        out["short_exit"] = False
        out["atr"] = atr(
            df["high"].to_numpy(float), df["low"].to_numpy(float), c, p["risk_atr_len"]
        )
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
