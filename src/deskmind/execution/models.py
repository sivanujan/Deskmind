"""Domain models and data structures for execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class WorkingType(StrEnum):
    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


@dataclass
class OrderInfo:
    """Standardized representation of an order across paper and testnet."""

    client_order_id: str
    symbol: str
    side: str  # BUY or SELL
    order_type: str  # MARKET, LIMIT, STOP_MARKET, TAKE_PROFIT_MARKET
    qty: Decimal
    order_id: str = ""
    price: Decimal = Decimal("0")
    stop_price: Decimal | None = None
    executed_qty: Decimal = Decimal("0")
    cum_quote: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    status: str = OrderStatus.NEW.value
    reduce_only: bool = False
    working_type: str = WorkingType.CONTRACT_PRICE.value
    created_at_ms: int = 0
    updated_at_ms: int = 0
    fee_paid: Decimal = Decimal("0")
    fee_asset: str = "USDT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "qty": str(self.qty),
            "price": str(self.price),
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "executed_qty": str(self.executed_qty),
            "cum_quote": str(self.cum_quote),
            "avg_price": str(self.avg_price),
            "status": self.status,
            "reduce_only": self.reduce_only,
            "working_type": self.working_type,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "fee_paid": str(self.fee_paid),
            "fee_asset": self.fee_asset,
        }


@dataclass
class PositionInfo:
    """Standardized representation of an open or flat position."""

    symbol: str
    side: Literal["long", "short", "flat"]
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    liquidation_price: Decimal
    leverage: int = 1
    margin_type: str = "isolated"
    isolated_margin: Decimal = Decimal("0")
    stop_loss_order_id: str | None = None
    stop_loss_price: Decimal | None = None
    take_profit_order_id: str | None = None
    take_profit_price: Decimal | None = None
    has_active_stop: bool = False
    stop_verified_at_ms: int = 0
    client_order_id: str = ""
    setup_id: str = ""
    opened_at_ms: int = 0

    @property
    def is_open(self) -> bool:
        return self.qty > Decimal("0") and self.side in ("long", "short")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "entry_price": str(self.entry_price),
            "mark_price": str(self.mark_price),
            "unrealized_pnl": str(self.unrealized_pnl),
            "liquidation_price": str(self.liquidation_price),
            "leverage": self.leverage,
            "margin_type": self.margin_type,
            "isolated_margin": str(self.isolated_margin),
            "stop_loss_order_id": self.stop_loss_order_id,
            "stop_loss_price": str(self.stop_loss_price)
            if self.stop_loss_price is not None
            else None,
            "take_profit_order_id": self.take_profit_order_id,
            "take_profit_price": str(self.take_profit_price)
            if self.take_profit_price is not None
            else None,
            "has_active_stop": self.has_active_stop,
            "stop_verified_at_ms": self.stop_verified_at_ms,
            "client_order_id": self.client_order_id,
            "setup_id": self.setup_id,
            "opened_at_ms": self.opened_at_ms,
        }


@dataclass
class BalanceInfo:
    """Standardized representation of account balances."""

    total_equity: Decimal
    available_balance: Decimal
    wallet_balance: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    margin_balance: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_equity": str(self.total_equity),
            "available_balance": str(self.available_balance),
            "wallet_balance": str(self.wallet_balance),
            "unrealized_pnl": str(self.unrealized_pnl),
            "margin_balance": str(self.margin_balance),
        }


@dataclass
class ExecutionResult:
    """Outcome of an execution request."""

    success: bool
    action: str  # "open", "close", "move_stop", "cancel_all"
    symbol: str
    order: OrderInfo | None = None
    stop_order: OrderInfo | None = None
    tp_order: OrderInfo | None = None
    error: str | None = None
    reason_code: str | None = None
    timestamp_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def take_profit_order(self) -> OrderInfo | None:
        return self.tp_order

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "symbol": self.symbol,
            "order": self.order.to_dict() if self.order else None,
            "stop_order": self.stop_order.to_dict() if self.stop_order else None,
            "tp_order": self.tp_order.to_dict() if self.tp_order else None,
            "error": self.error,
            "reason_code": self.reason_code,
            "timestamp_ms": self.timestamp_ms,
            "metadata": self.metadata,
        }
