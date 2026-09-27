"""Execution package for Binance USDT-M Futures testnet and paper trading."""

from deskmind.execution.approval import ApprovalGate, ApprovalRequest
from deskmind.execution.binance_client import BinanceFuturesClient
from deskmind.execution.binance_testnet import BinanceTestnetExecutor
from deskmind.execution.executor import BaseExecutor
from deskmind.execution.faults import FaultInjector
from deskmind.execution.journal import TradeJournalManager
from deskmind.execution.kill_switch import KillSwitch
from deskmind.execution.models import (
    BalanceInfo,
    ExecutionResult,
    OrderInfo,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionInfo,
    WorkingType,
)
from deskmind.execution.paper import PaperExecutor
from deskmind.execution.reconcile import Reconciler
from deskmind.execution.service import ExecutionService
from deskmind.execution.user_stream import UserStreamManager

__all__ = [
    "ApprovalGate",
    "ApprovalRequest",
    "BalanceInfo",
    "BaseExecutor",
    "BinanceFuturesClient",
    "BinanceTestnetExecutor",
    "ExecutionResult",
    "ExecutionService",
    "FaultInjector",
    "KillSwitch",
    "OrderInfo",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PaperExecutor",
    "PositionInfo",
    "Reconciler",
    "TradeJournalManager",
    "UserStreamManager",
    "WorkingType",
]
