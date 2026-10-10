"""MACD v1.0.0 (ported from the backtesting-trading-strategies skill).

Long when the MACD line crosses above its signal line; short when it crosses below. Each signal
reverses an opposite position. Fills at the next bar's open; protective stop 2 x ATR.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr, ema
from deskmind.strategy_lab.signal_rules import COMMON_PARAMS, act_on_signals, crossover, crossunder


class Strategy:
    """MACD signal-line crossover (long and short)."""

    name: str = "MACD"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {"fast": 12, "slow": 26, "signal": 9, **COMMON_PARAMS}

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def reset_state(self) -> None:
        pass

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"].to_numpy(float)
        macd_line = ema(c, p["fast"]) - ema(c, p["slow"])
        signal_line = ema(macd_line, p["signal"])
        out = df.copy()
        out["macd"], out["macd_signal"] = macd_line, signal_line
        out["long_entry"] = crossover(macd_line, signal_line)
        out["short_entry"] = crossunder(macd_line, signal_line)
        out["long_exit"] = False  # positions end on the opposite crossover or their stop
        out["short_exit"] = False
        out["atr"] = atr(
            df["high"].to_numpy(float), df["low"].to_numpy(float), c, p["risk_atr_len"]
        )
        return out

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        act_on_signals(i, ctx, broker, self.params)
