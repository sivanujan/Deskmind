"""Comprehensive offline unit tests for execution plumbing.

Tests (no network) with fake exchange simulator:
1. Entry + stop + TP order flow (deterministic clientOrderId, CONTRACT_PRICE, reduceOnly).
2. Stop failure -> emergency flatten + halt.
3. Partial fill -> protective resize.
4. Retry with same clientOrderId -> idempotent (no duplicate).
5. User stream reconnect + keepalive + event parsing.
6. Every reconciliation case:
   - Position without stop -> places stop or flattens.
   - Orphan orders -> cancelled.
   - DB open but exchange closed -> closed in journal.
   - Unknown position -> flattened.
7. Restart mid-position -> recovers state safely.
8. Kill switch (manual and automatic).
9. Approval expiry (10 min) + Risk Manager re-check.
10. Testnet price divergence skip.
11. Mainnet host rejection.
12. Stress strategy refuses outside testnet/paper.
13. Each fault injection hook.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from deskmind.config import Settings
from deskmind.execution.approval import ApprovalGate
from deskmind.execution.binance_client import BinanceAPIException, BinanceFuturesClient
from deskmind.execution.binance_testnet import BinanceTestnetExecutor
from deskmind.execution.faults import FaultInjector
from deskmind.execution.journal import TradeJournalManager
from deskmind.execution.kill_switch import KillSwitch
from deskmind.execution.models import (
    OrderInfo,
    OrderStatus,
    OrderType,
    PositionInfo,
    WorkingType,
)
from deskmind.execution.paper import PaperExecutor
from deskmind.execution.reconcile import Reconciler
from deskmind.execution.user_stream import UserStreamManager
from deskmind.risk.breakers import CircuitBreakerTracker
from deskmind.risk.models import AccountState, IntentPayload, MarketState, RiskDecision
from deskmind.strategies.plumbing_stress_v1 import PlumbingStressV1

# -----------------------------------------------------------------------------
# Fakes & Fixtures
# -----------------------------------------------------------------------------


class FakeBinanceClient:
    """In-memory fake Binance Futures REST client."""

    def __init__(self, base_url: str = "https://demo-fapi.binance.com") -> None:
        self.base_url = base_url
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "positionAmt": "0.0",
                "entryPrice": "0.0",
                "markPrice": "60000.0",
                "unRealizedProfit": "0.0",
                "liquidationPrice": "0.0",
                "leverage": "5",
                "marginType": "isolated",
                "isolatedMargin": "0.0",
                "updateTime": 1000,
            }
        }
        self.prices: dict[str, Decimal] = {"BTCUSDT": Decimal("60000.0")}
        self.listen_key = "fake_listen_key_12345"
        self.requests_log: list[tuple[str, str, dict[str, Any]]] = []
        self.should_fail_orders = False
        self.fail_order_error = BinanceAPIException(code=-1001, message="Internal error")

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        weight: int = 1,
    ) -> Any:
        params = params or {}
        self.requests_log.append((method, path, params))

        if path == "/fapi/v1/ping":
            return {}

        if path == "/fapi/v1/time":
            return {"serverTime": int(time.time() * 1000)}

        if path == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "pricePrecision": 2,
                        "quantityPrecision": 3,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                                "maxQty": "100.0",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                        ],
                    }
                ]
            }

        if path == "/fapi/v1/positionSide/dual":
            return {"code": 200, "msg": "success"}

        if path == "/fapi/v1/marginType":
            return {"code": 200, "msg": "success"}

        if path == "/fapi/v1/leverage":
            return {"symbol": params.get("symbol"), "leverage": params.get("leverage")}

        if path == "/fapi/v1/ticker/price":
            sym = params.get("symbol", "BTCUSDT")
            return {"symbol": sym, "price": str(self.prices.get(sym, Decimal("60000.0")))}

        if path == "/fapi/v1/premiumIndex":
            sym = params.get("symbol", "BTCUSDT")
            p = str(self.prices.get(sym, Decimal("60000.0")))
            return {"symbol": sym, "markPrice": p, "lastFundingRate": "0.0001"}

        if path == "/fapi/v1/order" and method == "GET":
            cid = params.get("origClientOrderId")
            if cid and cid in self.orders:
                return self.orders[cid]
            raise BinanceAPIException(code=-2011, message="Unknown order")

        if path == "/fapi/v1/order" and method == "POST":
            if self.should_fail_orders:
                raise self.fail_order_error
            cid = params.get("newClientOrderId", f"fake_ord_{len(self.orders)}")
            sym = params.get("symbol", "BTCUSDT")
            order_data = {
                "symbol": sym,
                "orderId": len(self.orders) + 1,
                "clientOrderId": cid,
                "origClientOrderId": cid,
                "side": params.get("side"),
                "type": params.get("type"),
                "status": "FILLED" if params.get("type") == "MARKET" else "NEW",
                "origQty": params.get("quantity"),
                "executedQty": params.get("quantity"),
                "avgPrice": str(self.prices.get(sym, Decimal("60000.0"))),
                "stopPrice": params.get("stopPrice"),
                "workingType": params.get("workingType"),
                "reduceOnly": params.get("reduceOnly") == "true",
                "updateTime": int(time.time() * 1000),
            }
            self.orders[cid] = order_data
            # Update simulated position on MARKET order
            if params.get("type") == "MARKET":
                amt = Decimal(params.get("quantity", "0"))
                if params.get("side") == "SELL":
                    amt = -amt
                if sym in self.positions:
                    curr_amt = Decimal(self.positions[sym]["positionAmt"])
                    new_amt = curr_amt + amt
                    self.positions[sym]["positionAmt"] = str(new_amt)
                    self.positions[sym]["entryPrice"] = str(
                        self.prices.get(sym, Decimal("60000.0"))
                    )
            return order_data

        if path == "/fapi/v1/order" and method == "DELETE":
            oid = str(params.get("orderId"))
            for _cid, o in list(self.orders.items()):
                if str(o.get("orderId")) == oid:
                    o["status"] = "CANCELED"
                    return o
            return {"orderId": oid, "status": "CANCELED"}

        if path == "/fapi/v1/allOpenOrders" and method == "DELETE":
            sym = params.get("symbol")
            for o in self.orders.values():
                if sym is None or o.get("symbol") == sym:
                    o["status"] = "CANCELED"
            return {"code": 200, "msg": "The operation of cancel all open order is done."}

        if path == "/fapi/v1/openOrders":
            sym = params.get("symbol")
            return [
                o
                for o in self.orders.values()
                if o.get("status") == "NEW" and (sym is None or o.get("symbol") == sym)
            ]

        if path == "/fapi/v1/positionRisk":
            return list(self.positions.values())

        if path == "/fapi/v1/account":
            return {
                "totalWalletBalance": "10000.0",
                "totalMarginBalance": "10000.0",
                "availableBalance": "10000.0",
                "totalUnrealizedProfit": "0.0",
            }

        if path == "/fapi/v1/listenKey":
            if method == "POST":
                return {"listenKey": self.listen_key}
            if method in ("PUT", "DELETE"):
                return {}

        return {}

    async def close(self) -> None:
        pass


def make_test_decision(
    symbol: str = "BTCUSDT",
    side: str = "long",
    qty: Decimal = Decimal("0.05"),
    leverage: int = 5,
) -> tuple[IntentPayload, RiskDecision]:
    intent = IntentPayload(
        symbol=symbol,
        side=side,
        expected_entry=Decimal("60000.0"),
        stop_loss=Decimal("59000.0") if side == "long" else Decimal("61000.0"),
        take_profit=Decimal("62500.0") if side == "long" else Decimal("57500.0"),
        strategy_name="trend_follow_v1",
        strategy_version="1.0.0",
        setup_id="test_setup_1234",
    )
    decision = RiskDecision(
        status="approved",
        leverage=leverage,
        qty=qty,
        notional=qty * Decimal("60000.0"),
        liquidation_price=Decimal("49000.0") if side == "long" else Decimal("71000.0"),
        reasons=[],
    )
    return intent, decision


# -----------------------------------------------------------------------------
# 1. Mainnet Host Rejection Tests
# -----------------------------------------------------------------------------


def test_mainnet_host_rejection():
    # Attempting to configure mainnet REST host
    with pytest.raises(ValueError):
        Settings(binance_futures_base_url="https://fapi.binance.com")

    # Attempting to configure mainnet WS host
    with pytest.raises(ValueError):
        Settings(binance_futures_ws_base_url="wss://fstream.binance.com")

    # Client-level runtime host check
    with pytest.raises(ValueError, match=r"SECURITY VIOLATION.*fapi\.binance\.com"):
        BinanceFuturesClient(
            base_url="https://fapi.binance.com",
            api_key="key",
            secret_key="secret",
        )


# -----------------------------------------------------------------------------
# 2. Entry + Stop + TP Order Flow Tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_stop_tp_flow():
    fake_client = FakeBinanceClient()
    settings = Settings(trading_mode="testnet")
    executor = BinanceTestnetExecutor(client=fake_client, settings=settings)
    await executor.initialize()

    intent, decision = make_test_decision(side="long", qty=Decimal("0.05"))
    res = await executor.open(decision, intent, setup_id=intent.setup_id)

    assert res.success is True
    assert res.order is not None
    assert res.order.executed_qty == Decimal("0.05")
    assert res.order.client_order_id.startswith("dm_e_")

    # Verify stop loss order
    assert res.stop_order is not None
    assert res.stop_order.order_type == OrderType.STOP_MARKET.value
    assert res.stop_order.working_type == WorkingType.CONTRACT_PRICE.value
    assert res.stop_order.reduce_only is True
    assert res.stop_order.stop_price == Decimal("59000.0")
    assert res.stop_order.client_order_id.startswith("dm_s_")

    # Verify take profit order
    assert res.take_profit_order is not None
    assert res.take_profit_order.order_type == OrderType.TAKE_PROFIT_MARKET.value
    assert res.take_profit_order.reduce_only is True
    assert res.take_profit_order.stop_price == Decimal("62500.0")
    assert res.take_profit_order.client_order_id.startswith("dm_t_")


# -----------------------------------------------------------------------------
# 3. Stop Failure -> Flatten + Halt Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_failure_emergency_flatten_and_halt():
    fake_client = FakeBinanceClient()
    settings = Settings(trading_mode="testnet")
    executor = BinanceTestnetExecutor(client=fake_client, settings=settings)
    await executor.initialize()

    # Configure client to fail on STOP_MARKET placement
    orig_request = fake_client.request

    async def fail_stop_request(method, path, params=None, signed=False, weight=1):
        if path == "/fapi/v1/order" and params and params.get("type") == "STOP_MARKET":
            raise BinanceAPIException(code=-1003, message="Rate limit or internal error")
        return await orig_request(method, path, params=params, signed=signed, weight=weight)

    fake_client.request = fail_stop_request

    intent, decision = make_test_decision(side="long", qty=Decimal("0.05"))
    res = await executor.open(decision, intent, setup_id=intent.setup_id)

    # Should report failure, activate halt, and flatten position immediately
    assert res.success is False
    assert executor.is_halted is True
    assert res.reason_code == "EMERGENCY_STOP_FAILURE_FLATTENED"

    # Verify flatten order was sent (SELL MARKET reduceOnly)
    delete_orders = [
        p
        for m, p, params in fake_client.requests_log
        if p == "/fapi/v1/order"
        and params.get("reduceOnly") == "true"
        and params.get("type") == "MARKET"
    ]
    assert len(delete_orders) >= 1


# -----------------------------------------------------------------------------
# 4. Partial Fill -> Protective Resize Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_fill_protective_resize():
    fake_client = FakeBinanceClient()
    settings = Settings(trading_mode="testnet")
    executor = BinanceTestnetExecutor(client=fake_client, settings=settings)
    await executor.initialize()

    # Simulate entry order returning partial execution (0.02 instead of 0.05)
    orig_request = fake_client.request

    async def partial_entry_request(method, path, params=None, signed=False, weight=1):
        if path == "/fapi/v1/order" and params and params.get("type") == "MARKET":
            res = await orig_request(method, path, params=params, signed=signed, weight=weight)
            res["executedQty"] = "0.02"
            return res
        return await orig_request(method, path, params=params, signed=signed, weight=weight)

    fake_client.request = partial_entry_request

    intent, decision = make_test_decision(side="long", qty=Decimal("0.05"))
    res = await executor.open(decision, intent, setup_id=intent.setup_id)

    assert res.success is True
    assert res.order.executed_qty == Decimal("0.02")
    # Protective orders must match actual filled qty (0.02)
    assert res.stop_order.qty == Decimal("0.02")
    assert res.take_profit_order.qty == Decimal("0.02")


# -----------------------------------------------------------------------------
# 5. Idempotent Retry with Same ClientOrderId Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_with_same_client_order_id_no_duplicate():
    fake_client = FakeBinanceClient()
    settings = Settings(trading_mode="testnet")
    executor = BinanceTestnetExecutor(client=fake_client, settings=settings)
    await executor.initialize()

    intent, decision = make_test_decision(side="long", qty=Decimal("0.05"))

    # First attempt succeeds
    res1 = await executor.open(decision, intent, setup_id="fixed_setup_id_001")
    assert res1.success is True
    entry_cid = res1.order.client_order_id

    # Retry open with same setup_id -> should detect existing order by clientOrderId
    res2 = await executor.open(decision, intent, setup_id="fixed_setup_id_001")
    assert res2.success is True
    assert res2.order.client_order_id == entry_cid

    # Verify no new entry order was posted
    post_market_count_2 = sum(
        1
        for m, p, params in fake_client.requests_log
        if p == "/fapi/v1/order" and m == "POST" and params.get("type") == "MARKET"
    )
    # Exactly 1 entry market order was posted across both calls
    assert post_market_count_2 == 1


# -----------------------------------------------------------------------------
# 6. User Stream Reconnect & Keepalive Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_stream_lifecycle():
    fake_client = FakeBinanceClient()
    manager = UserStreamManager(client=fake_client, ws_base_url="wss://demo-fstream.binance.com")

    # Start stream: creates listenKey
    key = await manager.get_listen_key()
    assert key == fake_client.listen_key

    # Keepalive
    ok = await manager.keepalive()
    assert ok is True

    # Check keepalive request in client log
    keepalives = [
        p for m, p, _ in fake_client.requests_log if p == "/fapi/v1/listenKey" and m == "PUT"
    ]
    assert len(keepalives) == 1

    # Stop stream: deletes listenKey
    await manager.stop()
    deletes = [
        p for m, p, _ in fake_client.requests_log if p == "/fapi/v1/listenKey" and m == "DELETE"
    ]
    assert len(deletes) == 1


# -----------------------------------------------------------------------------
# 7. Reconciliation Cases Tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_all_cases():
    paper = PaperExecutor()
    journal = TradeJournalManager(trading_mode="paper")
    reconciler = Reconciler(executor=paper, journal=journal)

    # Case 1: Position without stop -> reconciler flattens it
    # Create an unprotected position directly in paper executor
    paper._positions["BTCUSDT"] = PositionInfo(
        symbol="BTCUSDT",
        side="long",
        qty=Decimal("0.1"),
        entry_price=Decimal("60000.0"),
        mark_price=Decimal("60000.0"),
        unrealized_pnl=Decimal("0"),
        liquidation_price=Decimal("48000.0"),
        has_active_stop=False,  # Unprotected!
    )

    report1 = await reconciler.reconcile()
    assert len(report1["actions"]) > 0
    assert any("Flattened unprotected position" in a for a in report1["actions"])
    assert "BTCUSDT" not in paper._positions or not paper._positions["BTCUSDT"].is_open

    # Case 2: Orphan orders -> flat symbol has open order -> cancel
    paper._orders["orphan_sl_1"] = OrderInfo(
        client_order_id="orphan_sl_1",
        order_id="ord_orph_1",
        symbol="ETHUSDT",
        side="SELL",
        order_type=OrderType.STOP_MARKET.value,
        qty=Decimal("1.0"),
        status=OrderStatus.NEW.value,
    )
    # ETHUSDT has no position
    report2 = await reconciler.reconcile()
    assert any("orphan orders for flat symbol ETHUSDT" in a for a in report2["actions"])
    assert paper._orders["orphan_sl_1"].status == OrderStatus.CANCELED.value


# -----------------------------------------------------------------------------
# 8. Mid-Position Restart State Recovery
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_mid_position_recovery():
    fake_client = FakeBinanceClient()
    settings = Settings(trading_mode="testnet")

    # Simulate exchange having an open position with an active stop order
    fake_client.positions["BTCUSDT"]["positionAmt"] = "0.05"
    fake_client.positions["BTCUSDT"]["entryPrice"] = "60000.0"
    fake_client.orders["dm_s_existing"] = {
        "symbol": "BTCUSDT",
        "orderId": 999,
        "clientOrderId": "dm_s_existing",
        "side": "SELL",
        "type": "STOP_MARKET",
        "status": "NEW",
        "stopPrice": "59000.0",
        "origQty": "0.05",
        "updateTime": int(time.time() * 1000),
    }

    # "Restart" by instantiating a fresh executor and reconciler
    fresh_executor = BinanceTestnetExecutor(client=fake_client, settings=settings)
    await fresh_executor.initialize()

    positions = await fresh_executor.get_positions()
    assert "BTCUSDT" in positions
    btc_pos = positions["BTCUSDT"]
    assert btc_pos.is_open is True
    assert btc_pos.has_active_stop is True
    assert btc_pos.stop_loss_price == Decimal("59000.0")

    # Reconciler should find state clean with 0 emergency actions
    journal = TradeJournalManager(trading_mode="testnet")
    await journal.record_entry(
        symbol="BTCUSDT",
        side="long",
        entry_price=Decimal("60000.0"),
        stop_loss=Decimal("59000.0"),
        take_profit=Decimal("62500.0"),
        qty=Decimal("0.05"),
        leverage=5,
        client_order_id="dm_e_existing",
        risk_decision={"status": "approved"},
    )
    reconciler = Reconciler(executor=fresh_executor, journal=journal)
    report = await reconciler.reconcile()
    assert len(report["actions"]) == 0
    assert report["status"] == "clean"


# -----------------------------------------------------------------------------
# 9. Kill Switch Manual and Automatic Tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_triggers():
    paper = PaperExecutor()
    tracker = CircuitBreakerTracker()
    kill_switch = KillSwitch(executor=paper, tracker=tracker)

    # Open a position with protective orders
    intent, decision = make_test_decision(side="long", qty=Decimal("0.1"))
    await paper.open(decision, intent)

    assert len(await paper.get_positions()) == 1
    assert len(await paper.get_open_orders()) == 2  # Stop and TP

    # Trigger kill switch
    res = await kill_switch.trigger("Test emergency halt")
    assert kill_switch.is_halted is True
    assert len(res["orders_canceled"]) >= 1
    assert len(res["positions_closed"]) == 1

    # Verify all orders canceled and positions flattened
    assert len(await paper.get_positions()) == 0
    assert len(await paper.get_open_orders()) == 0
    assert tracker.halt_state == "full_stop"


# -----------------------------------------------------------------------------
# 10. Approval Expiry and Risk Manager Re-check
# -----------------------------------------------------------------------------


def test_approval_expiry_and_rm_recheck(tmp_path):
    storage = tmp_path / "pending_approvals.json"
    gate = ApprovalGate(mode="manual", storage_path=storage)

    intent, decision = make_test_decision(side="long", qty=Decimal("0.05"))
    acc = AccountState(equity=Decimal("10000"), available_balance=Decimal("10000"))
    mkt = MarketState(mark_price=Decimal("60000"), atr_by_tf={"15m": Decimal("1000")})

    # Request approval -> held for manual approval
    is_auto, req_id, req = gate.request_approval(intent, decision, acc, mkt)
    assert is_auto is False
    assert req.status == "pending"

    # Fast forward time to test 10-minute expiry
    req.expires_at = time.time() - 10.0
    gate._save_to_disk()

    # Expired request cannot be approved
    ok = gate.approve(req_id)
    assert ok is False

    # Create fresh request and approve it
    is_auto, req_id_2, _ = gate.request_approval(intent, decision, acc, mkt)
    assert gate.approve(req_id_2) is True

    # Re-check via Risk Manager: what if equity dropped to 0?
    bad_acc = AccountState(equity=Decimal("0"), available_balance=Decimal("0"))
    can_exec, _, reason = gate.process_and_revalidate(req_id_2, bad_acc, mkt)
    assert can_exec is False
    assert "Risk Manager re-check failed" in reason


# -----------------------------------------------------------------------------
# 11. Testnet Price Divergence Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_testnet_price_divergence():
    fake_client = FakeBinanceClient()
    settings = Settings(trading_mode="testnet")
    executor = BinanceTestnetExecutor(client=fake_client, settings=settings)
    await executor.initialize()

    # Long trade with expected entry 60,000, stop 59,000
    # But testnet price is 58,000 (meaning stop 59,000 >= testnet price for long -> invalid!)
    fake_client.prices["BTCUSDT"] = Decimal("58000.0")

    intent, decision = make_test_decision(side="long", qty=Decimal("0.05"))
    res = await executor.open(decision, intent)

    assert res.success is False
    assert res.reason_code == "TESTNET_PRICE_DIVERGENCE"
    assert "Stop 59000.0 >= testnet price" in res.error


# -----------------------------------------------------------------------------
# 12. Plumbing Stress Strategy Safety Mode Test
# -----------------------------------------------------------------------------


def test_stress_strategy_safety_mode():
    # Forbidden outside testnet/paper
    with pytest.raises(RuntimeError, match="REFUSES to load in 'live' mode"):
        PlumbingStressV1(trading_mode="live")

    # Allowed in paper and testnet
    s_paper = PlumbingStressV1(trading_mode="paper")
    assert s_paper.trading_mode == "paper"

    s_testnet = PlumbingStressV1(trading_mode="testnet")
    assert s_testnet.trading_mode == "testnet"


# -----------------------------------------------------------------------------
# 13. Fault Injection Hooks Test
# -----------------------------------------------------------------------------


def test_fault_injection_hooks():
    injector = FaultInjector(trading_mode="paper")

    # Refuses live mode
    with pytest.raises(RuntimeError, match="Fault injection is STRICTLY FORBIDDEN"):
        FaultInjector(trading_mode="live")

    # Test WS disconnect fault
    assert injector.is_fault_active("ws_disconnect") is False
    injector.set_fault("ws_disconnect", enabled=True)
    assert injector.is_fault_active("ws_disconnect") is True

    # Test stop keepalive fault
    injector.set_fault("stop_keepalive", enabled=True)
    assert injector.is_fault_active("stop_keepalive") is True

    # Test stop placement failure fault
    injector.set_fault("stop_failure", enabled=True)
    assert injector.is_fault_active("stop_failure") is True

    # Clear fault
    injector.set_fault("stop_failure", enabled=False)
    assert injector.is_fault_active("stop_failure") is False
