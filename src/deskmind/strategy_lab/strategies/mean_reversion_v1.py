"""Mean Reversion v1.0.0 (ported from the backtesting-trading-strategies skill).

Z-score of the close versus its SMA: long when z crosses below -z_threshold, short when it crosses
above +z_threshold, exit when z crosses back through 0. Fills at the next bar's open; protective
stop 2 x ATR. Uses Pine's population standard deviation.
"""

from __future__ import annotations

from typing import Any

import numpy as np
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
    """Z-score mean reversion (long and short)."""

    name: str = "Mean_Reversion"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"period": 20, "z_threshold": 2.0, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        mean, sd = sma(c, p["period"]), stdev(c, p["period"])
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(sd > 0, (c - mean) / sd, np.nan)
        n = len(c)
        zt = float(p["z_threshold"])
        zero = np.zeros(n)
        out = df.copy()
        out["zscore"] = z
        out["long_entry"] = crossunder(z, np.full(n, -zt))
        out["short_entry"] = crossover(z, np.full(n, zt))
        out["long_exit"] = crossover(z, zero)  # back up to the mean
        out["short_exit"] = crossunder(z, zero)  # back down to the mean
        out["atr"] = atr(
            df["high"].to_numpy(float), df["low"].to_numpy(float), c, p["risk_atr_len"]
        )
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
