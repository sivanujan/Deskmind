"""Strategy plugin interface for the Deskmind backtest engine.

Strategies receive market context at each bar close and return intents.
Strategies NEVER see future bars and NEVER handle fills, fees, or account balances directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

IntentAction = Literal["open_long", "open_short", "close", "move_stop"]
OrderType = Literal["market", "limit"]


@dataclass(frozen=True)
class Intent:
    symbol: str
    action: IntentAction
    order_type: OrderType = "market"
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    ranking_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountSnapshot:
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_position: PositionSnapshot | None = None


@dataclass
class PositionSnapshot:
    symbol: str
    side: Literal["long", "short"]
    entry_price: Decimal
    qty: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    entry_time_ms: int
    liquidation_price: Decimal
    spanned_gap: bool = False


@dataclass
class BarSnapshot:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class StrategyContext:
    symbol: str
    as_of_ms: int
    current_bar: BarSnapshot
    agent_report: dict[str, Any]
    account: AccountSnapshot


class BaseStrategy(ABC):
    name: str = "base"
    version: str = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}

    @abstractmethod
    def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
        """Called at each 5m bar close.

        contexts maps symbol -> StrategyContext.
        Returns a list of candidate Intents across all symbols.
        """
        ...
