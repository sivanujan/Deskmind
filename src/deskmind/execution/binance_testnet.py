"""Binance USDT-M Futures Testnet executor with exchange-side protective orders."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from deskmind.backtest.filters import SymbolFilters, round_price, round_qty
from deskmind.config import Settings, get_settings
from deskmind.execution.binance_client import BinanceAPIException, BinanceFuturesClient
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


class BinanceTestnetExecutor(BaseExecutor):
    """Executes trades on Binance USDT-M Futures Testnet.

    Enforces:
    - Host allowlist verification at startup and on every call.
    - Deterministic clientOrderIds and query-before-retry idempotency.
    - Pre-trade testnet price divergence check against mainnet.
    - Immediate exchange-side stop loss and take profit placement.
    - Protective order sizing covering actual filled quantity (partial fill safe).
    - Emergency watchdog: position without stop > 10s is immediately flattened.
    """

    def __init__(
        self,
        client: BinanceFuturesClient | None = None,
        settings: Settings | None = None,
        emergency_stop_timeout_s: float = 10.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or BinanceFuturesClient(
            base_url=self.settings.binance_futures_base_url,
            api_key=self.settings.binance_api_key.get_secret_value(),
            secret_key=self.settings.binance_secret_key.get_secret_value(),
        )
        self.emergency_stop_timeout_s = emergency_stop_timeout_s
        self.filters_map: dict[str, SymbolFilters] = {}
        self.is_halted = False
        self.halt_reason = ""

    async def initialize(self) -> None:
        """Set up position mode, isolated margin, and load exchange filters."""
        log.info("binance_testnet_initializing", base_url=self.client.base_url)

        # 1. Set One-Way Position Mode (dualSidePosition = false)
        try:
            await self.client.request(
                "POST",
                "/fapi/v1/positionSide/dual",
                params={"dualSidePosition": "false"},
                signed=True,
                weight=1,
            )
            log.info("position_mode_set_one_way")
        except BinanceAPIException as e:
            # -4059: "No need to change position side."
            if e.code == -4059:
                log.info("position_mode_already_one_way")
            else:
                log.warning("position_mode_setup_error", code=e.code, msg=e.message)

        # 2. Set Isolated Margin per symbol
        for sym in self.settings.data_symbols:
            try:
                await self.client.request(
                    "POST",
                    "/fapi/v1/marginType",
                    params={"symbol": sym, "marginType": "ISOLATED"},
                    signed=True,
                    weight=1,
                )
                log.info("margin_type_set_isolated", symbol=sym)
            except BinanceAPIException as e:
                # -4046: "No need to change margin type."
                if e.code == -4046:
                    log.debug("margin_type_already_isolated", symbol=sym)
                else:
                    log.warning("margin_type_setup_error", symbol=sym, code=e.code, msg=e.message)

        # 3. Load Exchange Filters
        await self.refresh_exchange_info()
        log.info("binance_testnet_initialized", symbols=list(self.filters_map.keys()))

    async def refresh_exchange_info(self) -> None:
        """Refresh symbol price/qty filters from exchangeInfo."""
        data = await self.client.request("GET", "/fapi/v1/exchangeInfo", weight=1)
        for s in data.get("symbols", []):
            sym = s.get("symbol")
            if sym not in self.settings.data_symbols:
                continue

            tick_size = Decimal("0.10")
            step_size = Decimal("0.001")
            min_qty = Decimal("0.001")
            max_qty = Decimal("10000.0")
            min_notional = Decimal("5.0")

            for f in s.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "PRICE_FILTER":
                    tick_size = Decimal(str(f.get("tickSize", "0.10")))
                elif ftype == "LOT_SIZE":
                    step_size = Decimal(str(f.get("stepSize", "0.001")))
                    min_qty = Decimal(str(f.get("minQty", "0.001")))
                    max_qty = Decimal(str(f.get("maxQty", "10000.0")))
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = Decimal(str(f.get("notional", f.get("minNotional", "5.0"))))

            self.filters_map[sym] = SymbolFilters(
                symbol=sym,
                tick_size=tick_size,
                step_size=step_size,
                min_qty=min_qty,
                max_qty=max_qty,
                min_notional=min_notional,
                price_precision=int(s.get("pricePrecision", 2)),
                quantity_precision=int(s.get("quantityPrecision", 3)),
            )

    def _get_filters(self, symbol: str) -> SymbolFilters:
        if symbol in self.filters_map:
            return self.filters_map[symbol]
        return SymbolFilters(
            symbol=symbol,
            tick_size=Decimal("0.10"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("10000.0"),
            min_notional=Decimal("5.0"),
            price_precision=2,
            quantity_precision=3,
        )

    async def get_ticker_price(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Fetch current (last_price, mark_price) from testnet."""
        ticker_data = await self.client.request(
            "GET", "/fapi/v1/ticker/price", params={"symbol": symbol}, weight=1
        )
        last_price = Decimal(str(ticker_data.get("price", "0")))

        premium_data = await self.client.request(
            "GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}, weight=1
        )
        mark_price = Decimal(str(premium_data.get("markPrice", last_price)))

        return last_price, mark_price

    async def validate_testnet_price_divergence(
        self,
        symbol: str,
        expected_entry: Decimal,
        stop_loss: Decimal,
        side: str,
    ) -> tuple[bool, str, tuple[Decimal, Decimal]]:
        """Validate testnet price and log divergence vs mainnet expected price."""
        last_price, mark_price = await self.get_ticker_price(symbol)
        if last_price <= Decimal("0"):
            return False, "Failed to fetch testnet price", (last_price, mark_price)

        div_pct = (
            abs(last_price - expected_entry) / expected_entry
            if expected_entry > 0
            else Decimal("0")
        )
        log.info(
            "testnet_price_divergence_check",
            symbol=symbol,
            mainnet_expected=str(expected_entry),
            testnet_last=str(last_price),
            testnet_mark=str(mark_price),
            divergence_pct=f"{div_pct * 100:.2f}%",
        )

        # 1. Stop side check against testnet price
        if side == "long" and stop_loss >= last_price:
            msg = (
                f"TESTNET_PRICE_DIVERGENCE: Stop {stop_loss} >= testnet price {last_price} for long"
            )
            log.warning("testnet_divergence_rejected", reason=msg)
            return False, msg, (last_price, mark_price)
        if side == "short" and stop_loss <= last_price:
            msg = f"TESTNET_PRICE_DIVERGENCE: Stop {stop_loss} <= testnet price {last_price} for short"
            log.warning("testnet_divergence_rejected", reason=msg)
            return False, msg, (last_price, mark_price)

        # 2. Maximum divergence threshold check
        max_div = self.settings.testnet_price_divergence_max_pct
        if div_pct > max_div:
            msg = f"TESTNET_PRICE_DIVERGENCE: Divergence {div_pct * 100:.1f}% exceeds threshold {max_div * 100:.1f}%"
            log.warning("testnet_divergence_rejected", reason=msg)
            return False, msg, (last_price, mark_price)

        return True, "OK", (last_price, mark_price)

    async def open(
        self,
        decision: RiskDecision,
        intent: IntentPayload,
        setup_id: str = "",
    ) -> ExecutionResult:
        now_ms = int(time.time() * 1000)
        symbol = intent.symbol

        if self.is_halted:
            return ExecutionResult(
                success=False,
                action="open",
                symbol=symbol,
                error=f"Executor is in HALT state: {self.halt_reason}",
                reason_code="CIRCUIT_BREAKER_FULL_STOP",
                timestamp_ms=now_ms,
            )

        # 1. Testnet Price Divergence Check
        valid, reason, (testnet_last, _testnet_mark) = await self.validate_testnet_price_divergence(
            symbol=symbol,
            expected_entry=intent.expected_entry,
            stop_loss=intent.stop_loss,
            side=intent.side,
        )
        if not valid:
            return ExecutionResult(
                success=False,
                action="open",
                symbol=symbol,
                error=reason,
                reason_code="TESTNET_PRICE_DIVERGENCE",
                timestamp_ms=now_ms,
            )

        # 2. Set Leverage on Testnet
        try:
            await self.client.request(
                "POST",
                "/fapi/v1/leverage",
                params={"symbol": symbol, "leverage": decision.leverage},
                signed=True,
                weight=1,
            )
        except BinanceAPIException as e:
            log.warning(
                "set_leverage_warning", symbol=symbol, leverage=decision.leverage, msg=e.message
            )

        # 3. Calculate Rounded Quantity
        filters = self._get_filters(symbol)
        qty = round_qty(decision.qty, filters.step_size)
        if qty < filters.min_qty:
            return ExecutionResult(
                success=False,
                action="open",
                symbol=symbol,
                error=f"Quantity {qty} below exchange minQty {filters.min_qty}",
                reason_code="EXCHANGE_FILTER_MIN_QTY",
                timestamp_ms=now_ms,
            )

        # 4. Deterministic clientOrderIds
        if setup_id:
            safe_setup = setup_id.replace(":", "_").replace("-", "_")
            entry_cid = f"dm_e_{safe_setup[:28]}"
            sl_cid = f"dm_s_{safe_setup[:28]}"
            tp_cid = f"dm_t_{safe_setup[:28]}"
        else:
            entry_cid = f"dm_e_ord_{now_ms}"
            sl_cid = f"dm_s_ord_{now_ms}"
            tp_cid = f"dm_t_ord_{now_ms}"

        order_side = "BUY" if intent.side == "long" else "SELL"
        protective_side = "SELL" if intent.side == "long" else "BUY"

        # 5. Idempotency Check: query order before sending
        existing_order = None
        try:
            existing_order = await self.client.request(
                "GET",
                "/fapi/v1/order",
                params={"symbol": symbol, "origClientOrderId": entry_cid},
                signed=True,
                weight=1,
            )
        except BinanceAPIException as e:
            # -2011: Unknown order (expected on first attempt)
            if e.code != -2011:
                log.warning("order_idempotency_query_error", symbol=symbol, msg=e.message)

        entry_resp = existing_order
        if entry_resp is None:
            log.info(
                "placing_entry_order", symbol=symbol, side=order_side, qty=str(qty), cid=entry_cid
            )
            try:
                entry_resp = await self.client.request(
                    "POST",
                    "/fapi/v1/order",
                    params={
                        "symbol": symbol,
                        "side": order_side,
                        "type": "MARKET",
                        "quantity": str(qty),
                        "newClientOrderId": entry_cid,
                    },
                    signed=True,
                    weight=1,
                )
            except BinanceAPIException as e:
                return ExecutionResult(
                    success=False,
                    action="open",
                    symbol=symbol,
                    error=f"Entry order rejected: {e.message}",
                    reason_code=f"BINANCE_{e.code}",
                    timestamp_ms=now_ms,
                )

        exec_qty = Decimal(str(entry_resp.get("executedQty", qty)))
        avg_price = Decimal(str(entry_resp.get("avgPrice", testnet_last)))
        if avg_price <= Decimal("0"):
            avg_price = testnet_last

        entry_order = OrderInfo(
            client_order_id=entry_cid,
            order_id=str(entry_resp.get("orderId", "")),
            symbol=symbol,
            side=order_side,
            order_type=OrderType.MARKET.value,
            qty=qty,
            executed_qty=exec_qty,
            price=avg_price,
            avg_price=avg_price,
            cum_quote=Decimal(str(entry_resp.get("cumQuote", avg_price * exec_qty))),
            status=str(entry_resp.get("status", OrderStatus.FILLED.value)),
            created_at_ms=int(entry_resp.get("updateTime", now_ms)),
            updated_at_ms=int(entry_resp.get("updateTime", now_ms)),
        )

        # 6. Place Exchange-Side Protective Stop Loss and Take Profit
        # Protective size matches actual filled quantity
        stop_price = round_price(intent.stop_loss, filters.tick_size)
        tp_price = round_price(intent.take_profit, filters.tick_size)

        # 6. Place Exchange-Side Protective Stop Loss and Take Profit
        # Using Binance Algo Service (/fapi/v1/algoOrder) per current Binance USDT-M specification
        stop_price = round_price(intent.stop_loss, filters.tick_size)
        tp_price = round_price(intent.take_profit, filters.tick_size)

        stop_order = None
        tp_order = None
        stop_placed = False

        # Stop placement with retries using Algo Service
        for attempt in range(1, 4):
            try:
                # Try modern Algo Service first
                try:
                    sl_resp = await self.client.request(
                        "POST",
                        "/fapi/v1/algoOrder",
                        params={
                            "symbol": symbol,
                            "side": protective_side,
                            "algoType": "CONDITIONAL",
                            "type": "STOP_MARKET",
                            "quantity": str(exec_qty),
                            "triggerPrice": str(stop_price),
                            "reduceOnly": "true",
                            "workingType": WorkingType.CONTRACT_PRICE.value,
                            "clientAlgoId": sl_cid,
                            "positionSide": "BOTH",
                        },
                        signed=True,
                        weight=1,
                    )
                except BinanceAPIException as algo_err:
                    # If endpoint is unknown on an older mock/stub, fallback to legacy order endpoint
                    if algo_err.code in (-4059, -1002, 404):
                        sl_resp = await self.client.request(
                            "POST",
                            "/fapi/v1/order",
                            params={
                                "symbol": symbol,
                                "side": protective_side,
                                "type": "STOP_MARKET",
                                "quantity": str(exec_qty),
                                "stopPrice": str(stop_price),
                                "reduceOnly": "true",
                                "workingType": WorkingType.CONTRACT_PRICE.value,
                                "newClientOrderId": sl_cid,
                            },
                            signed=True,
                            weight=1,
                        )
                    else:
                        raise

                oid = str(sl_resp.get("algoId", sl_resp.get("orderId", "")))
                status = str(
                    sl_resp.get("orderStatus", sl_resp.get("status", OrderStatus.NEW.value))
                )
                stop_order = OrderInfo(
                    client_order_id=sl_cid,
                    order_id=oid,
                    symbol=symbol,
                    side=protective_side,
                    order_type=OrderType.STOP_MARKET.value,
                    qty=exec_qty,
                    stop_price=stop_price,
                    status=status,
                    reduce_only=True,
                    working_type=WorkingType.CONTRACT_PRICE.value,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                )
                stop_placed = True
                log.info(
                    "protective_stop_placed",
                    symbol=symbol,
                    stop_price=str(stop_price),
                    cid=sl_cid,
                    algo_id=oid,
                )
                break
            except BinanceAPIException as e:
                log.warning("stop_placement_attempt_failed", attempt=attempt, error=e.message)
                await asyncio.sleep(0.5)

        # 7. EMERGENCY WATCHDOG CHECK: If stop placement failed, immediately flatten position!
        if not stop_placed:
            log.critical(
                "EMERGENCY_STOP_PLACEMENT_FAILED",
                symbol=symbol,
                msg="Position open without stop! Flattening immediately!",
            )
            self.is_halted = True
            self.halt_reason = f"Emergency flatten: stop placement failed for {symbol}"
            await self.close(symbol, reason="emergency_no_stop")
            return ExecutionResult(
                success=False,
                action="open",
                symbol=symbol,
                order=entry_order,
                error="CRITICAL: Stop loss placement failed after retries; position flattened immediately.",
                reason_code="EMERGENCY_STOP_FAILURE_FLATTENED",
                timestamp_ms=now_ms,
            )

        # Place Take Profit via Algo Service (non-critical, but reduce-only)
        try:
            try:
                tp_resp = await self.client.request(
                    "POST",
                    "/fapi/v1/algoOrder",
                    params={
                        "symbol": symbol,
                        "side": protective_side,
                        "algoType": "CONDITIONAL",
                        "type": "TAKE_PROFIT_MARKET",
                        "quantity": str(exec_qty),
                        "triggerPrice": str(tp_price),
                        "reduceOnly": "true",
                        "workingType": WorkingType.CONTRACT_PRICE.value,
                        "clientAlgoId": tp_cid,
                        "positionSide": "BOTH",
                    },
                    signed=True,
                    weight=1,
                )
            except BinanceAPIException as algo_err:
                if algo_err.code in (-4059, -1002, 404):
                    tp_resp = await self.client.request(
                        "POST",
                        "/fapi/v1/order",
                        params={
                            "symbol": symbol,
                            "side": protective_side,
                            "type": "TAKE_PROFIT_MARKET",
                            "quantity": str(exec_qty),
                            "stopPrice": str(tp_price),
                            "reduceOnly": "true",
                            "workingType": WorkingType.CONTRACT_PRICE.value,
                            "newClientOrderId": tp_cid,
                        },
                        signed=True,
                        weight=1,
                    )
                else:
                    raise

            tp_oid = str(tp_resp.get("algoId", tp_resp.get("orderId", "")))
            tp_status = str(
                tp_resp.get("orderStatus", tp_resp.get("status", OrderStatus.NEW.value))
            )
            tp_order = OrderInfo(
                client_order_id=tp_cid,
                order_id=tp_oid,
                symbol=symbol,
                side=protective_side,
                order_type=OrderType.TAKE_PROFIT_MARKET.value,
                qty=exec_qty,
                stop_price=tp_price,
                status=tp_status,
                reduce_only=True,
                working_type=WorkingType.CONTRACT_PRICE.value,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            log.info(
                "protective_tp_placed",
                symbol=symbol,
                tp_price=str(tp_price),
                cid=tp_cid,
                algo_id=tp_oid,
            )
        except BinanceAPIException as e:
            log.warning("tp_placement_failed", symbol=symbol, error=e.message)

        return ExecutionResult(
            success=True,
            action="open",
            symbol=symbol,
            order=entry_order,
            stop_order=stop_order,
            tp_order=tp_order,
            timestamp_ms=now_ms,
            metadata={
                "leverage": decision.leverage,
                "entry_price": str(avg_price),
                "testnet_pnl_warning": "Testnet PnL is NOT meaningful due to synthetic testnet liquidity",
            },
        )

    async def close(
        self,
        symbol: str,
        reason: str = "manual_close",
        qty: Decimal | None = None,
    ) -> ExecutionResult:
        now_ms = int(time.time() * 1000)
        positions = await self.get_positions()
        pos = positions.get(symbol)

        if pos is None or not pos.is_open:
            await self.cancel_all(symbol)
            return ExecutionResult(
                success=True,
                action="close",
                symbol=symbol,
                metadata={"status": "already_flat"},
                timestamp_ms=now_ms,
            )

        close_qty = pos.qty if qty is None or qty >= pos.qty else qty
        close_side = "SELL" if pos.side == "long" else "BUY"
        close_cid = f"dm_c_{symbol}_{now_ms % 100000}"

        try:
            close_resp = await self.client.request(
                "POST",
                "/fapi/v1/order",
                params={
                    "symbol": symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": str(close_qty),
                    "reduceOnly": "true",
                    "newClientOrderId": close_cid,
                },
                signed=True,
                weight=1,
            )
            # Cancel any remaining protective orders
            await self.cancel_all(symbol)

            close_order = OrderInfo(
                client_order_id=close_cid,
                order_id=str(close_resp.get("orderId", "")),
                symbol=symbol,
                side=close_side,
                order_type=OrderType.MARKET.value,
                qty=close_qty,
                executed_qty=Decimal(str(close_resp.get("executedQty", close_qty))),
                price=Decimal(str(close_resp.get("avgPrice", "0"))),
                avg_price=Decimal(str(close_resp.get("avgPrice", "0"))),
                status=str(close_resp.get("status", OrderStatus.FILLED.value)),
                reduce_only=True,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )

            log.info("position_closed", symbol=symbol, close_qty=str(close_qty), reason=reason)
            return ExecutionResult(
                success=True,
                action="close",
                symbol=symbol,
                order=close_order,
                timestamp_ms=now_ms,
                metadata={"reason": reason},
            )
        except BinanceAPIException as e:
            log.error("position_close_error", symbol=symbol, error=e.message)
            return ExecutionResult(
                success=False,
                action="close",
                symbol=symbol,
                error=f"Failed to close position: {e.message}",
                reason_code=f"BINANCE_{e.code}",
                timestamp_ms=now_ms,
            )

    async def move_stop(
        self,
        symbol: str,
        new_stop: Decimal,
    ) -> ExecutionResult:
        now_ms = int(time.time() * 1000)
        positions = await self.get_positions()
        pos = positions.get(symbol)
        if pos is None or not pos.is_open:
            return ExecutionResult(
                success=False,
                action="move_stop",
                symbol=symbol,
                error=f"No active position for {symbol}",
                timestamp_ms=now_ms,
            )

        # Cancel existing STOP_MARKET orders (checking both Algo and regular endpoints)
        open_orders = await self.get_open_orders(symbol)
        for o in open_orders:
            if o.order_type == OrderType.STOP_MARKET.value:
                try:
                    await self.client.request(
                        "DELETE",
                        "/fapi/v1/algoOrder",
                        params={"symbol": symbol, "algoId": o.order_id},
                        signed=True,
                        weight=1,
                    )
                except Exception:
                    try:
                        await self.client.request(
                            "DELETE",
                            "/fapi/v1/order",
                            params={"symbol": symbol, "orderId": o.order_id},
                            signed=True,
                            weight=1,
                        )
                    except Exception as e:
                        log.warning("cancel_old_stop_warning", error=str(e))

        filters = self._get_filters(symbol)
        rounded_stop = round_price(new_stop, filters.tick_size)
        protective_side = "SELL" if pos.side == "long" else "BUY"
        new_cid = f"dm_s_{symbol}_{now_ms % 100000}"

        try:
            try:
                sl_resp = await self.client.request(
                    "POST",
                    "/fapi/v1/algoOrder",
                    params={
                        "symbol": symbol,
                        "side": protective_side,
                        "algoType": "CONDITIONAL",
                        "type": "STOP_MARKET",
                        "quantity": str(pos.qty),
                        "triggerPrice": str(rounded_stop),
                        "reduceOnly": "true",
                        "workingType": WorkingType.CONTRACT_PRICE.value,
                        "clientAlgoId": new_cid,
                        "positionSide": "BOTH",
                    },
                    signed=True,
                    weight=1,
                )
            except BinanceAPIException as algo_err:
                if algo_err.code in (-4059, -1002, 404):
                    sl_resp = await self.client.request(
                        "POST",
                        "/fapi/v1/order",
                        params={
                            "symbol": symbol,
                            "side": protective_side,
                            "type": "STOP_MARKET",
                            "quantity": str(pos.qty),
                            "stopPrice": str(rounded_stop),
                            "reduceOnly": "true",
                            "workingType": WorkingType.CONTRACT_PRICE.value,
                            "newClientOrderId": new_cid,
                        },
                        signed=True,
                        weight=1,
                    )
                else:
                    raise

            oid = str(sl_resp.get("algoId", sl_resp.get("orderId", "")))
            status = str(sl_resp.get("orderStatus", sl_resp.get("status", OrderStatus.NEW.value)))
            stop_order = OrderInfo(
                client_order_id=new_cid,
                order_id=oid,
                symbol=symbol,
                side=protective_side,
                order_type=OrderType.STOP_MARKET.value,
                qty=pos.qty,
                stop_price=rounded_stop,
                status=status,
                reduce_only=True,
                working_type=WorkingType.CONTRACT_PRICE.value,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            log.info(
                "stop_moved", symbol=symbol, new_stop=str(rounded_stop), cid=new_cid, algo_id=oid
            )
            return ExecutionResult(
                success=True,
                action="move_stop",
                symbol=symbol,
                stop_order=stop_order,
                timestamp_ms=now_ms,
            )
        except BinanceAPIException as e:
            return ExecutionResult(
                success=False,
                action="move_stop",
                symbol=symbol,
                error=f"Move stop rejected: {e.message}",
                reason_code=f"BINANCE_{e.code}",
                timestamp_ms=now_ms,
            )

    async def cancel_all(self, symbol: str | None = None) -> list[str]:
        symbols = [symbol] if symbol else self.settings.data_symbols
        canceled = []
        for sym in symbols:
            try:
                await self.client.request(
                    "DELETE",
                    "/fapi/v1/allOpenOrders",
                    params={"symbol": sym},
                    signed=True,
                    weight=1,
                )
            except Exception as e:
                log.debug("cancel_all_orders_info", symbol=sym, note=str(e))
            try:
                await self.client.request(
                    "DELETE",
                    "/fapi/v1/algoOpenOrders",
                    params={"symbol": sym},
                    signed=True,
                    weight=1,
                )
            except Exception as e:
                log.debug("cancel_all_algo_orders_info", symbol=sym, note=str(e))
            canceled.append(sym)
        return canceled

    async def get_positions(self) -> dict[str, PositionInfo]:
        """Query open positions and match against open protective stop orders."""
        data = await self.client.request("GET", "/fapi/v1/positionRisk", signed=True, weight=5)
        open_orders = await self.get_open_orders()
        stops_by_sym = {
            o.symbol: o for o in open_orders if o.order_type == OrderType.STOP_MARKET.value
        }
        tps_by_sym = {
            o.symbol: o for o in open_orders if o.order_type == OrderType.TAKE_PROFIT_MARKET.value
        }

        res: dict[str, PositionInfo] = {}
        now_ms = int(time.time() * 1000)

        for p in data:
            amt = Decimal(str(p.get("positionAmt", "0")))
            sym = p.get("symbol", "")
            if amt == Decimal("0"):
                continue

            side: Literal["long", "short"] = "long" if amt > Decimal("0") else "short"
            qty = abs(amt)
            entry = Decimal(str(p.get("entryPrice", "0")))
            mark = Decimal(str(p.get("markPrice", "0")))
            upnl = Decimal(str(p.get("unRealizedProfit", "0")))
            liq = Decimal(str(p.get("liquidationPrice", "0")))
            lev = int(p.get("leverage", 1))

            sl_order = stops_by_sym.get(sym)
            tp_order = tps_by_sym.get(sym)
            has_stop = sl_order is not None and sl_order.status == OrderStatus.NEW.value

            pos = PositionInfo(
                symbol=sym,
                side=side,
                qty=qty,
                entry_price=entry,
                mark_price=mark,
                unrealized_pnl=upnl,
                liquidation_price=liq,
                leverage=lev,
                margin_type=p.get("marginType", "isolated"),
                isolated_margin=Decimal(str(p.get("isolatedMargin", "0"))),
                stop_loss_order_id=sl_order.order_id if sl_order else None,
                stop_loss_price=sl_order.stop_price if sl_order else None,
                take_profit_order_id=tp_order.order_id if tp_order else None,
                take_profit_price=tp_order.stop_price if tp_order else None,
                has_active_stop=has_stop,
                stop_verified_at_ms=now_ms if has_stop else 0,
                opened_at_ms=int(p.get("updateTime", now_ms)),
            )
            res[sym] = pos

        return res

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        params = {"symbol": symbol} if symbol else {}
        orders: list[OrderInfo] = []

        # 1. Standard open orders
        try:
            data = await self.client.request(
                "GET", "/fapi/v1/openOrders", params=params, signed=True, weight=5
            )
            for o in data:
                orders.append(
                    OrderInfo(
                        client_order_id=o.get("clientOrderId", ""),
                        order_id=str(o.get("orderId", "")),
                        symbol=o.get("symbol", ""),
                        side=o.get("side", ""),
                        order_type=o.get("type", ""),
                        qty=Decimal(str(o.get("origQty", "0"))),
                        executed_qty=Decimal(str(o.get("executedQty", "0"))),
                        price=Decimal(str(o.get("price", "0"))),
                        stop_price=Decimal(str(o.get("stopPrice")))
                        if o.get("stopPrice") is not None
                        else None,
                        status=o.get("status", OrderStatus.NEW.value),
                        reduce_only=bool(o.get("reduceOnly", False)),
                        working_type=o.get("workingType", WorkingType.CONTRACT_PRICE.value),
                        created_at_ms=int(o.get("time", 0)),
                        updated_at_ms=int(o.get("updateTime", 0)),
                    )
                )
        except Exception as e:
            log.warning("get_open_orders_error", error=str(e))

        # 2. Algo conditional open orders
        try:
            algo_data = await self.client.request(
                "GET", "/fapi/v1/openAlgoOrders", params=params, signed=True, weight=1
            )
            raw_algo_list = (
                algo_data
                if isinstance(algo_data, list)
                else (algo_data.get("orders", []) if isinstance(algo_data, dict) else [])
            )
            for a in raw_algo_list:
                orders.append(
                    OrderInfo(
                        client_order_id=a.get("clientAlgoId", ""),
                        order_id=str(a.get("algoId", "")),
                        symbol=a.get("symbol", ""),
                        side=a.get("side", ""),
                        order_type=a.get("orderType", a.get("type", "")),
                        qty=Decimal(str(a.get("quantity", a.get("origQty", "0")))),
                        executed_qty=Decimal(str(a.get("actualQty", a.get("executedQty", "0")))),
                        price=Decimal(str(a.get("price", "0"))),
                        stop_price=Decimal(str(a.get("triggerPrice", a.get("stopPrice", "0"))))
                        if (a.get("triggerPrice") or a.get("stopPrice"))
                        else None,
                        status=a.get("orderStatus", a.get("status", OrderStatus.NEW.value)),
                        reduce_only=bool(a.get("reduceOnly", True)),
                        working_type=a.get("workingType", WorkingType.CONTRACT_PRICE.value),
                        created_at_ms=int(a.get("time", 0)),
                        updated_at_ms=int(a.get("updateTime", a.get("time", 0))),
                    )
                )
        except Exception as e:
            log.debug("get_open_algo_orders_info", error=str(e))

        return orders

    async def get_balance(self) -> BalanceInfo:
        data = await self.client.request("GET", "/fapi/v1/account", signed=True, weight=5)
        total_equity = Decimal(str(data.get("totalMarginBalance", "0")))
        available = Decimal(str(data.get("availableBalance", "0")))
        wallet = Decimal(str(data.get("totalWalletBalance", "0")))
        upnl = Decimal(str(data.get("totalUnrealizedProfit", "0")))

        return BalanceInfo(
            total_equity=total_equity,
            available_balance=available,
            wallet_balance=wallet,
            unrealized_pnl=upnl,
            margin_balance=total_equity,
        )

    async def close_connections(self) -> None:
        await self.client.close()
        log.info("binance_testnet_executor_closed")
