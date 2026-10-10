"""SMA Crossover v1.0.0 (ported from the backtesting-trading-strategies skill).

Long when the fast SMA crosses above the slow SMA (golden cross); exit when it crosses back
below (death cross). Long only. Fills at the next bar's open; protective stop 2 x ATR.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr, sma
from deskmind.strategy_lab.signal_rules import COMMON_PARAMS, act_on_signals, crossover, crossunder


class Strategy:
    """Simple moving average crossover (long only)."""

    name: str = "SMA_Crossover"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"fast_period": 20, "slow_period": 50, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        fast, slow = sma(c, p["fast_period"]), sma(c, p["slow_period"])
        out = df.copy()
        out["fast_ma"], out["slow_ma"] = fast, slow
        out["long_entry"] = crossover(fast, slow)
        out["long_exit"] = crossunder(fast, slow)
        out["short_entry"] = False
        out["short_exit"] = False
        out["atr"] = atr(
            df["high"].to_numpy(float), df["low"].to_numpy(float), c, p["risk_atr_len"]
        )
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
