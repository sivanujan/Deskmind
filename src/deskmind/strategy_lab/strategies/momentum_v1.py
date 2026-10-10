"""Momentum v1.0.0 (ported from the backtesting-trading-strategies skill).

Rate of change over `period` bars: long when ROC crosses above `threshold` %; exit when ROC turns
negative. Long only. Fills at the next bar's open; protective stop 2 x ATR. ROC is Pine's
ta.roc (versus the close `period` bars ago).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr
from deskmind.strategy_lab.signal_rules import (
    COMMON_PARAMS,
    act_on_signals,
    crossover,
    crossunder,
    roc,
)


class Strategy:
    """Rate-of-change momentum (long only)."""

    name: str = "Momentum"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"period": 14, "threshold": 5.0, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        r = roc(c, int(p["period"]))
        n = len(c)
        out = df.copy()
        out["roc"] = r
        out["long_entry"] = crossover(r, np.full(n, float(p["threshold"])))
        out["long_exit"] = crossunder(r, np.zeros(n))
        out["short_entry"] = False
        out["short_exit"] = False
        out["atr"] = atr(
            df["high"].to_numpy(float), df["low"].to_numpy(float), c, p["risk_atr_len"]
        )
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
