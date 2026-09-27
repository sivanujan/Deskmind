"""Base executor interface for Paper and Binance Testnet execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deskmind.execution.models import BalanceInfo, ExecutionResult, OrderInfo, PositionInfo
    from deskmind.risk.models import IntentPayload, RiskDecision


class BaseExecutor(ABC):
    """Abstract base executor interface.

    Both PaperExecutor and BinanceTestnetExecutor implement this exact interface,
    allowing strategies, the Risk Manager, and reconciliation to operate identically
    in paper and testnet modes.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Perform idempotent account and symbol setup (margins, leverage, filters)."""

    @abstractmethod
    async def open(
        self,
        decision: RiskDecision,
        intent: IntentPayload,
        setup_id: str = "",
    ) -> ExecutionResult:
        """Execute an entry order, followed immediately by protective exchange-side SL and TP."""

    @abstractmethod
    async def close(
        self,
        symbol: str,
        reason: str = "manual_close",
        qty: Decimal | None = None,
    ) -> ExecutionResult:
        """Close an active position immediately using a reduce-only market order."""

    @abstractmethod
    async def move_stop(
        self,
        symbol: str,
        new_stop: Decimal,
    ) -> ExecutionResult:
        """Cancel the existing stop-loss and place a new exchange-side stop-loss."""

    @abstractmethod
    async def cancel_all(
        self,
        symbol: str | None = None,
    ) -> list[str]:
        """Cancel all open orders for a specific symbol or all symbols."""

    @abstractmethod
    async def get_positions(self) -> dict[str, PositionInfo]:
        """Fetch current active positions by symbol."""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        """Fetch all currently open (pending / conditional) orders."""

    @abstractmethod
    async def get_balance(self) -> BalanceInfo:
        """Fetch current account balances and total equity."""

    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Return (last_price, mark_price) for the symbol."""

    @abstractmethod
    async def close_connections(self) -> None:
        """Cleanly terminate any active network sessions or background tasks."""
