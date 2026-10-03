"""Paper executor simulating fills on live data with identical backtest friction models."""

from __future__ import annotations

from decimal import Decimal
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from deskmind.backtest.fills import calc_slippage_pct, simulate_candle_exit, simulate_market_fill
from deskmind.backtest.filters import SymbolFilters, load_symbol_filters, round_qty
from deskmind.backtest.liquidation import compute_liquidation_price
from deskmind.config import (
    DEFAULT_INITIAL_CASH,
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_SLIPPAGE_ATR_FACTOR,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_TAKER_FEE_RATE,
)
from deskmind.execution.executor import BaseExecutor
from deskmind.execution.models import (
    BalanceInfo,
    ExecutionResult,
    OrderInfo,
    OrderStatus,
    OrderType,
    PositionInfo,
    WorkingType,
)
from deskmind.logging import get_logger

if TYPE_CHECKING:
    from deskmind.risk.models import IntentPayload, RiskDecision

log = get_logger(__name__)


class PaperExecutor(BaseExecutor):
    """Simulated execution engine maintaining in-memory exchange state.

    Uses exact same fee, slippage, and liquidation calculation as the backtest engine.
    """

    def __init__(
        self,
        initial_cash: Decimal = DEFAULT_INITIAL_CASH,
        maker_fee_rate: Decimal = DEFAULT_MAKER_FEE_RATE,
        taker_fee_rate: Decimal = DEFAULT_TAKER_FEE_RATE,
        slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS,
        slippage_atr_factor: Decimal = DEFAULT_SLIPPAGE_ATR_FACTOR,
        filters_map: dict[str, SymbolFilters] | None = None,
    ) -> None:
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.slippage_bps = slippage_bps
        self.slippage_atr_factor = slippage_atr_factor
        self.filters_map = filters_map or {}

        # In-memory exchange state
        self._positions: dict[str, PositionInfo] = {}
        self._orders: dict[str, OrderInfo] = {}  # client_order_id -> OrderInfo
        self._prices: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (last, mark)
        self._atrs: dict[str, Decimal] = {}  # symbol -> atr

    def set_price(
        self, symbol: str, last_price: Decimal, mark_price: Decimal, atr: Decimal | None = None
    ) -> None:
        """Update live ticker price used for paper fills and mark-to-market."""
        self._prices[symbol] = (last_price, mark_price)
        if atr is not None:
            self._atrs[symbol] = atr

        # Mark-to-market open position
        if symbol in self._positions:
            pos = self._positions[symbol]
            if pos.is_open:
                pos.mark_price = mark_price
                if pos.side == "long":
                    pos.unrealized_pnl = (mark_price - pos.entry_price) * pos.qty
                else:
                    pos.unrealized_pnl = (pos.entry_price - mark_price) * pos.qty

    async def initialize(self) -> None:
        """Initialize paper executor (idempotent)."""
        log.info("paper_executor_initialized", cash=str(self.cash))

    def _get_filters(self, symbol: str) -> SymbolFilters:
        if symbol in self.filters_map:
            return self.filters_map[symbol]
        return load_symbol_filters(Path("data"), symbol)

    def _calc_slippage_pct(self, price: Decimal, atr: Decimal | None) -> Decimal:
        return calc_slippage_pct(price, atr, self.slippage_bps, self.slippage_atr_factor)

    async def get_ticker_price(self, symbol: str) -> tuple[Decimal, Decimal]:
        if symbol in self._prices:
            return self._prices[symbol]
        return Decimal("100.0"), Decimal("100.0")

    async def open(
        self,
        decision: RiskDecision,
        intent: IntentPayload,
        setup_id: str = "",
    ) -> ExecutionResult:
        now_ms = int(time.time() * 1000)
        symbol = intent.symbol
        filters = self._get_filters(symbol)

        # Ensure single position constraint in Phase 1
        for existing_sym, existing_pos in self._positions.items():
            if existing_pos.is_open:
                return ExecutionResult(
                    success=False,
                    action="open",
                    symbol=symbol,
                    error=f"Position already open for {existing_sym}",
                    reason_code="PORTFOLIO_MAX_POSITIONS",
                    timestamp_ms=now_ms,
                )

        qty = round_qty(decision.qty, filters.step_size)
        if qty <= Decimal("0"):
            return ExecutionResult(
                success=False,
                action="open",
                symbol=symbol,
                error="Quantity rounded to zero",
                reason_code="POSITION_SIZE_ZERO",
                timestamp_ms=now_ms,
            )

        last_price, mark_price = await self.get_ticker_price(symbol)
        if last_price <= Decimal("0"):
            last_price = intent.expected_entry

        atr = self._atrs.get(symbol, last_price * Decimal("0.02"))
        side_str = "BUY" if intent.side == "long" else "SELL"
        fill_price, _slip_pct = simulate_market_fill(
            price=last_price,
            side=side_str,
            tick_size=filters.tick_size,
            atr=atr,
            slippage_bps=self.slippage_bps,
            slippage_atr_factor=self.slippage_atr_factor,
        )

        # Deterministic clientOrderId from setup_id
        if setup_id:
            safe_setup = setup_id.replace(":", "_").replace("-", "_")
            entry_cid = f"dm_e_{safe_setup[:28]}"
            sl_cid = f"dm_s_{safe_setup[:28]}"
            tp_cid = f"dm_t_{safe_setup[:28]}"
        else:
            entry_cid = f"dm_e_ord_{now_ms}"
            sl_cid = f"dm_s_ord_{now_ms}"
            tp_cid = f"dm_t_ord_{now_ms}"

        # Deduct entry fee
        fee = fill_price * qty * self.taker_fee_rate
        self.cash -= fee

        entry_order = OrderInfo(
            client_order_id=entry_cid,
            order_id=f"paper_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=side_str,
            order_type=OrderType.MARKET.value,
            qty=qty,
            executed_qty=qty,
            price=fill_price,
            avg_price=fill_price,
            cum_quote=fill_price * qty,
            status=OrderStatus.FILLED.value,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            fee_paid=fee,
        )
        self._orders[entry_cid] = entry_order

        # Liquidation price calculation
        liq_price = compute_liquidation_price(
            entry_price=fill_price,
            qty=qty,
            side=intent.side,
            leverage=decision.leverage,
        )

        # Place protective exchange-side Stop Loss and Take Profit
        protective_side = "SELL" if intent.side == "long" else "BUY"

        stop_order = OrderInfo(
            client_order_id=sl_cid,
            order_id=f"paper_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=protective_side,
            order_type=OrderType.STOP_MARKET.value,
            qty=qty,
            stop_price=intent.stop_loss,
            status=OrderStatus.NEW.value,
            reduce_only=True,
            working_type=WorkingType.CONTRACT_PRICE.value,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        self._orders[sl_cid] = stop_order

        tp_order = OrderInfo(
            client_order_id=tp_cid,
            order_id=f"paper_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=protective_side,
            order_type=OrderType.TAKE_PROFIT_MARKET.value,
            qty=qty,
            stop_price=intent.take_profit,
            status=OrderStatus.NEW.value,
            reduce_only=True,
            working_type=WorkingType.CONTRACT_PRICE.value,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        self._orders[tp_cid] = tp_order

        # Record active position
        pos = PositionInfo(
            symbol=symbol,
            side=intent.side,
            qty=qty,
            entry_price=fill_price,
            mark_price=mark_price,
            unrealized_pnl=Decimal("0"),
            liquidation_price=liq_price,
            leverage=decision.leverage,
            margin_type="isolated",
            isolated_margin=(fill_price * qty) / Decimal(str(decision.leverage)),
            stop_loss_order_id=stop_order.order_id,
            stop_loss_price=intent.stop_loss,
            take_profit_order_id=tp_order.order_id,
            take_profit_price=intent.take_profit,
            has_active_stop=True,
            stop_verified_at_ms=now_ms,
            client_order_id=entry_cid,
            setup_id=setup_id,
            opened_at_ms=now_ms,
        )
        self._positions[symbol] = pos

        log.info(
            "paper_order_opened",
            symbol=symbol,
            side=intent.side,
            qty=str(qty),
            fill_price=str(fill_price),
            stop=str(intent.stop_loss),
            tp=str(intent.take_profit),
            client_order_id=entry_cid,
        )

        return ExecutionResult(
            success=True,
            action="open",
            symbol=symbol,
            order=entry_order,
            stop_order=stop_order,
            tp_order=tp_order,
            timestamp_ms=now_ms,
            metadata={"leverage": decision.leverage, "liquidation_price": str(liq_price)},
        )

    async def close(
        self,
        symbol: str,
        reason: str = "manual_close",
        qty: Decimal | None = None,
        explicit_exit_price: Decimal | None = None,
        is_maker: bool = False,
    ) -> ExecutionResult:
        now_ms = int(time.time() * 1000)
        pos = self._positions.get(symbol)
        if pos is None or not pos.is_open:
            return ExecutionResult(
                success=True,
                action="close",
                symbol=symbol,
                metadata={"status": "already_flat"},
                timestamp_ms=now_ms,
            )

        close_qty = pos.qty if qty is None or qty >= pos.qty else qty
        filters = self._get_filters(symbol)
        last_price, _ = await self.get_ticker_price(symbol)
        atr = self._atrs.get(symbol, last_price * Decimal("0.02"))

        close_side = "SELL" if pos.side == "long" else "BUY"
        if explicit_exit_price is not None:
            fill_price = explicit_exit_price
        else:
            fill_price, _ = simulate_market_fill(
                price=last_price,
                side=close_side,
                tick_size=filters.tick_size,
                atr=atr,
                slippage_bps=self.slippage_bps,
                slippage_atr_factor=self.slippage_atr_factor,
            )

        if close_side == "SELL":
            gross_pnl = (fill_price - pos.entry_price) * close_qty
        else:
            gross_pnl = (pos.entry_price - fill_price) * close_qty

        fee_rate = self.maker_fee_rate if is_maker else self.taker_fee_rate
        fee = fill_price * close_qty * fee_rate
        net_pnl = gross_pnl - fee
        self.cash += gross_pnl - fee

        close_cid = f"dm_c_{symbol}_{now_ms}"
        close_order = OrderInfo(
            client_order_id=close_cid,
            order_id=f"paper_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET.value,
            qty=close_qty,
            executed_qty=close_qty,
            price=fill_price,
            avg_price=fill_price,
            cum_quote=fill_price * close_qty,
            status=OrderStatus.FILLED.value,
            reduce_only=True,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            fee_paid=fee,
        )
        self._orders[close_cid] = close_order

        # Cancel remaining protective orders for this symbol
        await self.cancel_all(symbol)

        if close_qty >= pos.qty:
            pos.side = "flat"
            pos.qty = Decimal("0")
            pos.has_active_stop = False
            pos.stop_loss_order_id = None
            pos.take_profit_order_id = None
        else:
            pos.qty -= close_qty

        log.info(
            "paper_order_closed",
            symbol=symbol,
            close_qty=str(close_qty),
            fill_price=str(fill_price),
            pnl=str(net_pnl),
            reason=reason,
        )

        return ExecutionResult(
            success=True,
            action="close",
            symbol=symbol,
            order=close_order,
            timestamp_ms=now_ms,
            metadata={"pnl": str(net_pnl), "reason": reason},
        )

    async def move_stop(
        self,
        symbol: str,
        new_stop: Decimal,
    ) -> ExecutionResult:
        now_ms = int(time.time() * 1000)
        pos = self._positions.get(symbol)
        if pos is None or not pos.is_open:
            return ExecutionResult(
                success=False,
                action="move_stop",
                symbol=symbol,
                error=f"No active position for {symbol}",
                timestamp_ms=now_ms,
            )

        # Cancel old stop
        for _cid, o in list(self._orders.items()):
            if (
                o.symbol == symbol
                and o.order_type == OrderType.STOP_MARKET.value
                and o.status == OrderStatus.NEW.value
            ):
                o.status = OrderStatus.CANCELED.value

        new_cid = f"dm_s_{symbol}_{now_ms}"
        protective_side = "SELL" if pos.side == "long" else "BUY"
        new_stop_order = OrderInfo(
            client_order_id=new_cid,
            order_id=f"paper_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=protective_side,
            order_type=OrderType.STOP_MARKET.value,
            qty=pos.qty,
            stop_price=new_stop,
            status=OrderStatus.NEW.value,
            reduce_only=True,
            working_type=WorkingType.CONTRACT_PRICE.value,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        self._orders[new_cid] = new_stop_order
        pos.stop_loss_order_id = new_stop_order.order_id
        pos.stop_loss_price = new_stop
        pos.has_active_stop = True
        pos.stop_verified_at_ms = now_ms

        log.info("paper_stop_moved", symbol=symbol, new_stop=str(new_stop))
        return ExecutionResult(
            success=True,
            action="move_stop",
            symbol=symbol,
            stop_order=new_stop_order,
            timestamp_ms=now_ms,
        )

    async def cancel_all(self, symbol: str | None = None) -> list[str]:
        canceled = []
        for _cid, o in list(self._orders.items()):
            if (symbol is None or o.symbol == symbol) and o.status == OrderStatus.NEW.value:
                o.status = OrderStatus.CANCELED.value
                canceled.append(o.client_order_id)
        return canceled

    async def get_positions(self) -> dict[str, PositionInfo]:
        return {sym: pos for sym, pos in self._positions.items() if pos.is_open}

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        return [
            o
            for o in self._orders.values()
            if o.status == OrderStatus.NEW.value and (symbol is None or o.symbol == symbol)
        ]

    async def get_balance(self) -> BalanceInfo:
        unrealized = sum(
            (p.unrealized_pnl for p in self._positions.values() if p.is_open),
            Decimal("0"),
        )
        equity = self.cash + unrealized
        return BalanceInfo(
            total_equity=equity,
            available_balance=self.cash,
            wallet_balance=self.cash,
            unrealized_pnl=unrealized,
            margin_balance=equity,
        )

    async def check_triggers_on_candle(
        self,
        symbol: str,
        high: Decimal,
        low: Decimal,
        timestamp_ms: int,
    ) -> list[ExecutionResult]:
        """Simulate high/low crossing protective stop or take profit orders."""
        results = []
        pos = self._positions.get(symbol)
        if pos is None or not pos.is_open:
            return results

        filters = self._get_filters(symbol)
        last_price, _ = await self.get_ticker_price(symbol)
        atr = self._atrs.get(symbol, last_price * Decimal("0.02"))

        exit_price, exit_reason, is_maker, _ = simulate_candle_exit(
            pos_side=pos.side,
            stop_loss=pos.stop_loss_price,
            take_profit=pos.take_profit_price,
            b_open=last_price,
            b_high=high,
            b_low=low,
            tick_size=filters.tick_size,
            atr=atr,
            slippage_bps=self.slippage_bps,
            slippage_atr_factor=self.slippage_atr_factor,
        )

        if exit_reason is not None and exit_price is not None:
            res = await self.close(
                symbol,
                reason=exit_reason,
                explicit_exit_price=exit_price,
                is_maker=is_maker,
            )
            results.append(res)

        return results

    async def close_connections(self) -> None:
        """Paper executor cleanup."""
        log.info("paper_executor_closed")
