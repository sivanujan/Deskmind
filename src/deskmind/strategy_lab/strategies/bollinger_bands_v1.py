"""Bollinger Bands v1.0.0 (ported from the backtesting-trading-strategies skill).

Mean reversion: long when the close crosses below the lower band, short when it crosses above
the upper band, exit when the close crosses the middle band. Fills at the next bar's open;
protective stop 2 x ATR. Bands use Pine's population standard deviation.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr, sma
from deskmind.strategy_lab.signal_rules import (
    COMMON_PARAMS,
    act_on_signals,
    crossover,
    crossunder,
    stdev,
)


class Strategy:
    """Bollinger Bands mean reversion (long and short)."""

    name: str = "Bollinger_Bands"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"period": 20, "std_dev": 2.0, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        mid = sma(c, p["period"])
        dev = stdev(c, p["period"]) * float(p["std_dev"])
        upper, lower = mid + dev, mid - dev
        out = df.copy()
        out["bb_mid"], out["bb_upper"], out["bb_lower"] = mid, upper, lower
        out["long_entry"] = crossunder(c, lower)
        out["short_entry"] = crossover(c, upper)
        out["long_exit"] = crossover(c, mid)  # back up to the middle band
        out["short_exit"] = crossunder(c, mid)  # back down to the middle band
        out["atr"] = atr(
            df["high"].to_numpy(float), df["low"].to_numpy(float), c, p["risk_atr_len"]
        )
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
