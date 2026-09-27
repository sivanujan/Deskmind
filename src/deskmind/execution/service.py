"""Execution Service runtime for Deskmind.

Orchestrates:
- Live/Testnet Strategy -> Risk Manager -> Approval Gate -> Executor -> Trade Journal
- Periodic reconciliation (startup + every 60s)
- Emergency unstopped-position watchdog (< 10s rule)
- User data stream keepalive & event processing
- Kill switch monitoring
- Health & status reporting
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from deskmind.config import Settings
from deskmind.execution.approval import ApprovalGate
from deskmind.execution.binance_client import BinanceFuturesClient
from deskmind.execution.binance_testnet import BinanceTestnetExecutor
from deskmind.execution.executor import BaseExecutor
from deskmind.execution.journal import TradeJournalManager
from deskmind.execution.kill_switch import KillSwitch
from deskmind.execution.paper import PaperExecutor
from deskmind.execution.reconcile import Reconciler
from deskmind.execution.user_stream import UserStreamManager
from deskmind.risk.breakers import CircuitBreakerTracker
from deskmind.risk.config import RiskConfig
from deskmind.risk.evaluator import evaluate
from deskmind.risk.models import AccountState, IntentPayload, MarketState, RiskDecision
from deskmind.strategies.base import (
    AccountSnapshot,
    BarSnapshot,
    BaseStrategy,
    Intent,
    PositionSnapshot,
    StrategyContext,
)
from deskmind.strategies.plumbing_stress_v1 import PlumbingStressV1
from deskmind.strategies.trend_follow_v1 import TrendFollowV1

logger = logging.getLogger(__name__)

STATUS_FILE = Path("data/trade/trade_status.json")


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        executor: BaseExecutor | None = None,
        strategy: BaseStrategy | None = None,
        strategy_name: str = "trend_follow_v1",
        risk_config: RiskConfig | None = None,
        status_file: Path = STATUS_FILE,
    ) -> None:
        self.settings = settings
        self.trading_mode = settings.trading_mode.lower()
        self.approval_mode = settings.approval_mode.lower()
        self.status_file = status_file
        self.risk_config = risk_config or RiskConfig()

        if self.trading_mode not in ("paper", "testnet"):
            raise ValueError(
                f"TRADING_MODE '{self.trading_mode}' forbidden. Must be 'paper' or 'testnet'."
            )

        # Initialize Executor
        if executor:
            self.executor = executor
        elif self.trading_mode == "paper":
            self.executor = PaperExecutor(initial_cash=Decimal("10000"))
        else:
            client = BinanceFuturesClient(
                api_key=settings.binance_api_key.get_secret_value()
                if settings.binance_api_key
                else "",
                secret_key=settings.binance_secret_key.get_secret_value()
                if settings.binance_secret_key
                else "",
                base_url=settings.binance_futures_base_url,
            )
            self.executor = BinanceTestnetExecutor(client=client, settings=settings)

        # Initialize Strategy
        if strategy:
            self.strategy = strategy
        elif strategy_name == "plumbing_stress_v1":
            self.strategy = PlumbingStressV1(trading_mode=self.trading_mode)
        else:
            self.strategy = TrendFollowV1()

        # Circuit Breakers & Kill Switch
        self.breakers = CircuitBreakerTracker(config=self.risk_config)
        self.kill_switch = KillSwitch(
            executor=self.executor,
            tracker=self.breakers,
        )

        # Journal & Approval
        self.journal = TradeJournalManager(trading_mode=self.trading_mode)
        self.approval_gate = ApprovalGate(
            mode=self.approval_mode,
            trading_mode=self.trading_mode,
            risk_config=self.risk_config,
        )

        # Reconciler
        self.reconciler = Reconciler(
            executor=self.executor,
            journal=self.journal,
        )

        # User stream (testnet only)
        self.user_stream: UserStreamManager | None = None
        if self.trading_mode == "testnet" and isinstance(self.executor, BinanceTestnetExecutor):
            self.user_stream = UserStreamManager(
                client=self.executor.client,
                ws_base_url=settings.binance_futures_ws_base_url,
            )

        # Internal state & stats
        self.is_running = False
        self._stop_event = asyncio.Event()
        self.recent_reports: dict[str, dict[str, Any]] = {}
        self.recent_prices: dict[str, Decimal] = {}

        # 24h counters
        self.stats = {
            "start_time": time.time(),
            "intents_count": 0,
            "approvals_count": 0,
            "orders_sent": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "reconciliation_fixes": 0,
            "last_order_time": 0.0,
            "last_report_time": 0.0,
            "last_reconcile_time": 0.0,
        }

        # Track unstopped positions for emergency 10s rule
        self._unstopped_since: dict[str, float] = {}

    async def start(self) -> None:
        """Start the execution service and background tasks."""
        logger.info(
            "Starting Deskmind ExecutionService (mode: %s, approval: %s, strategy: %s)",
            self.trading_mode,
            self.approval_mode,
            self.strategy.name,
        )
        self.is_running = True
        self._stop_event.clear()

        # Initialize executor
        await self.executor.initialize()

        # Connect user stream if applicable
        if self.user_stream:
            await self.user_stream.start()

        # Initial reconciliation
        reconcile_report = await self.reconciler.reconcile()
        self.stats["reconciliation_fixes"] += len(reconcile_report.get("actions", []))
        self.stats["last_reconcile_time"] = time.time()

        # Start background loops
        tasks = [
            asyncio.create_task(self._reconciliation_loop(), name="reconcile_loop"),
            asyncio.create_task(self._emergency_stop_watchdog(), name="stop_watchdog"),
            asyncio.create_task(self._pending_approval_checker(), name="approval_checker"),
            asyncio.create_task(self._status_update_loop(), name="status_loop"),
            asyncio.create_task(self._agent_report_listener(), name="report_listener"),
        ]

        if isinstance(self.executor, PaperExecutor):
            tasks.append(
                asyncio.create_task(self._paper_candle_tick_loop(), name="paper_tick_loop")
            )

        try:
            await self._stop_event.wait()
        finally:
            logger.info("Stopping ExecutionService tasks...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.user_stream:
                await self.user_stream.stop()
            await self.executor.close_connections()
            self.is_running = False
            await self._write_status()
            logger.info("ExecutionService stopped gracefully.")

    def stop(self) -> None:
        """Signal the service to stop."""
        self._stop_event.set()

    async def _agent_report_listener(self) -> None:
        """Listen to Redis channel 'deskmind:agent_report'."""
        try:
            from deskmind.cache.redis_client import create_redis

            redis = create_redis(self.settings)
            pubsub = redis.pubsub()
            await pubsub.subscribe("deskmind:agent_report")
            logger.info("Subscribed to Redis channel 'deskmind:agent_report'")
            while not self._stop_event.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        report_data = json.loads(msg["data"])
                        await self.handle_agent_report(report_data)
                    except Exception as e:
                        logger.error("Error handling agent_report: %s", e)
        except Exception as e:
            logger.warning(
                "Redis not available for agent_report stream: %s. Will rely on direct calls/polling.",
                e,
            )
            while not self._stop_event.is_set():
                await asyncio.sleep(1.0)

    async def handle_agent_report(self, report: dict[str, Any]) -> None:
        """Handle incoming agent report for a symbol."""
        symbol = report.get("symbol")
        if not symbol:
            return

        self.recent_reports[symbol] = report
        self.stats["last_report_time"] = time.time()

        # If kill switch is halted, don't process new entries
        if self.kill_switch.is_halted:
            return

        # Prepare strategy context
        contexts: dict[str, StrategyContext] = {}
        positions = await self.executor.get_positions()
        pos_by_sym = positions if isinstance(positions, dict) else {p.symbol: p for p in positions}
        balance = await self.executor.get_balance()

        for sym, rep in self.recent_reports.items():
            pos = pos_by_sym.get(sym)
            pos_snap = None
            if pos and pos.qty > Decimal("0"):
                pos_snap = PositionSnapshot(
                    symbol=pos.symbol,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    qty=pos.qty,
                    stop_loss=pos.stop_loss_price,
                    take_profit=pos.take_profit_price,
                    entry_time_ms=pos.opened_at_ms,
                    liquidation_price=pos.liquidation_price or Decimal("0"),
                )

            account_snap = AccountSnapshot(
                cash=balance.available_balance,
                equity=balance.total_equity,
                realized_pnl=Decimal("0"),
                unrealized_pnl=balance.unrealized_pnl,
                open_position=pos_snap,
            )

            # Get current mark/last price
            try:
                last_p, _mark_p = await self.executor.get_ticker_price(sym)
                curr_price = last_p
                self.recent_prices[sym] = curr_price
            except Exception:
                curr_price = self.recent_prices.get(sym, Decimal("100"))

            bar_snap = BarSnapshot(
                open_time=rep.get("as_of", int(time.time() * 1000)),
                close_time=rep.get("as_of", int(time.time() * 1000)),
                open=curr_price,
                high=curr_price * Decimal("1.001"),
                low=curr_price * Decimal("0.999"),
                close=curr_price,
                volume=Decimal("100"),
            )

            contexts[sym] = StrategyContext(
                symbol=sym,
                as_of_ms=rep.get("as_of", int(time.time() * 1000)),
                current_bar=bar_snap,
                agent_report=rep,
                account=account_snap,
            )

        # Run strategy
        intents = self.strategy.on_bar(contexts)
        for intent in intents:
            if intent.action in ("open_long", "open_short"):
                await self._process_intent(intent, balance)

    async def _process_intent(self, intent: Intent, balance: Any) -> None:
        self.stats["intents_count"] += 1
        side = "long" if intent.action == "open_long" else "short"
        curr_price = self.recent_prices.get(intent.symbol, intent.stop_loss or Decimal("100"))

        intent_payload = IntentPayload(
            symbol=intent.symbol,
            side=side,
            expected_entry=curr_price,
            stop_loss=intent.stop_loss or Decimal("0"),
            take_profit=intent.take_profit or Decimal("0"),
            strategy_name=self.strategy.name,
            strategy_version=self.strategy.version,
            setup_id=intent.setup_id,
        )

        account_state = AccountState(
            equity=balance.total_equity,
            available_balance=balance.available_balance,
        )

        tf_1h = self.recent_reports.get(intent.symbol, {}).get("timeframes", {}).get("1h", {})
        atr = Decimal(str(tf_1h.get("atr", "1.0")))
        market_state = MarketState(
            mark_price=curr_price,
            atr=atr,
            tick_size=Decimal("0.10"),
            step_size=Decimal("0.001"),
        )

        # Evaluate through Risk Manager
        decision = evaluate(intent_payload, account_state, market_state, self.risk_config)
        logger.info(
            "RiskManager evaluation for %s %s: approved=%s, qty=%s, leverage=%s",
            intent.symbol,
            side,
            decision.approved,
            decision.qty,
            decision.leverage,
        )

        if not decision.approved:
            self.stats["orders_rejected"] += 1
            failing = [r.message for r in decision.reasons if not r.passed]
            logger.info("Trade rejected by Risk Manager: %s", "; ".join(failing))
            return

        # Pass to ApprovalGate
        is_auto, _req_id, req = self.approval_gate.request_approval(
            intent_payload, decision, account_state, market_state
        )

        if is_auto:
            self.stats["approvals_count"] += 1
            await self._execute_trade(req.intent, req.decision)

    async def _execute_trade(self, intent: IntentPayload, decision: RiskDecision) -> None:
        """Send approved trade to executor and record in journal."""
        self.stats["orders_sent"] += 1
        self.stats["last_order_time"] = time.time()

        res = await self.executor.open(
            decision=decision,
            intent=intent,
            setup_id=intent.setup_id,
        )

        if res.success and res.order:
            self.stats["orders_filled"] += 1
            logger.info(
                "Trade EXECUTED: %s %s qty=%s fill_price=%s",
                intent.symbol,
                intent.side,
                res.order.executed_qty,
                res.order.avg_price,
            )
            # Record in journal
            await self.journal.record_intent(intent, decision)
            await self.journal.record_order(res.order)
            if res.stop_order:
                await self.journal.record_order(res.stop_order)
            if res.take_profit_order:
                await self.journal.record_order(res.take_profit_order)
        else:
            self.stats["orders_rejected"] += 1
            logger.error("Trade execution FAILED: %s", res.error)

    async def _pending_approval_checker(self) -> None:
        """Check for manual approvals and re-check with Risk Manager before firing."""
        while not self._stop_event.is_set():
            try:
                for req in list(self.approval_gate._requests.values()):
                    if req.status == "approved" and not req.resolved_at:
                        # User approved via CLI!
                        balance = await self.executor.get_balance()
                        last_p, _mark_p = await self.executor.get_ticker_price(req.intent.symbol)
                        account_state = AccountState(
                            equity=balance.total_equity,
                            available_balance=balance.available_balance,
                        )
                        market_state = MarketState(
                            mark_price=last_p,
                            atr=req.market.atr,
                            tick_size=req.market.tick_size,
                            step_size=req.market.step_size,
                        )
                        can_exec, new_dec, reason = self.approval_gate.process_and_revalidate(
                            req.id, account_state, market_state
                        )
                        if can_exec and new_dec:
                            self.stats["approvals_count"] += 1
                            await self._execute_trade(req.intent, new_dec)
                        else:
                            logger.warning(
                                "Approved trade %s cancelled on re-validation: %s", req.id, reason
                            )
            except Exception as e:
                logger.error("Error in pending approval checker: %s", e)
            await asyncio.sleep(2.0)

    async def _emergency_stop_watchdog(self) -> None:
        """Safety rule: Open position without active exchange stop for > 10s is an emergency.

        Must immediately close via reduce-only market order, log CRITICAL, and halt.
        """
        while not self._stop_event.is_set():
            try:
                positions_dict = await self.executor.get_positions()
                now = time.time()
                active_symbols = set()

                for sym, pos in positions_dict.items():
                    if pos.qty > Decimal("0") and pos.is_open:
                        active_symbols.add(sym)
                        if not pos.has_active_stop:
                            if sym not in self._unstopped_since:
                                self._unstopped_since[sym] = now
                                logger.warning(
                                    "Position %s has NO active stop! Watchdog timer started.", sym
                                )
                            else:
                                elapsed = now - self._unstopped_since[sym]
                                if elapsed > 10.0:
                                    logger.critical(
                                        "EMERGENCY: Position %s has had NO active stop for %.1fs (>10s)! "
                                        "Flattening immediately and triggering HALT!",
                                        sym,
                                        elapsed,
                                    )
                                    await self.executor.close(
                                        sym, reason="EMERGENCY_UNSTOPPED_POSITION_10S"
                                    )
                                    await self.kill_switch.trigger_halt(
                                        f"Emergency stop watchdog: {sym} unstopped for > 10s"
                                    )
                                    self._unstopped_since.pop(sym, None)
                        else:
                            self._unstopped_since.pop(sym, None)

                # Clear closed positions
                for s in list(self._unstopped_since.keys()):
                    if s not in active_symbols:
                        self._unstopped_since.pop(s, None)

            except Exception as e:
                logger.error("Error in emergency stop watchdog: %s", e)
            await asyncio.sleep(2.0)

    async def _reconciliation_loop(self) -> None:
        """Run state reconciliation every 60 seconds."""
        while not self._stop_event.is_set():
            await asyncio.sleep(60.0)
            try:
                report = await self.reconciler.reconcile()
                self.stats["reconciliation_fixes"] += len(report.get("actions", []))
                self.stats["last_reconcile_time"] = time.time()
            except Exception as e:
                logger.error("Error during scheduled reconciliation: %s", e)

    async def _paper_candle_tick_loop(self) -> None:
        """If running PaperExecutor, simulate candle ticks against open positions."""
        if not isinstance(self.executor, PaperExecutor):
            return
        while not self._stop_event.is_set():
            try:
                for sym, price in list(self.recent_prices.items()):
                    await self.executor.check_triggers_on_candle(
                        sym,
                        high=price * Decimal("1.002"),
                        low=price * Decimal("0.998"),
                        timestamp_ms=int(time.time() * 1000),
                    )
            except Exception as e:
                logger.debug("Paper tick update error: %s", e)
            await asyncio.sleep(1.0)

    async def _status_update_loop(self) -> None:
        """Periodically write status to disk."""
        while not self._stop_event.is_set():
            try:
                await self._write_status()
            except Exception as e:
                logger.debug("Failed writing status: %s", e)
            await asyncio.sleep(5.0)

    async def _write_status(self) -> None:
        """Write current execution status for CLI consumption."""
        now = time.time()
        positions_dict = await self.executor.get_positions()
        pos_list = (
            list(positions_dict.values()) if isinstance(positions_dict, dict) else positions_dict
        )
        open_orders = await self.executor.get_open_orders()
        balance = await self.executor.get_balance()

        stream_health = "disconnected"
        if self.user_stream:
            stream_health = "connected" if self.user_stream.is_connected else "disconnected"
        elif self.trading_mode == "paper":
            stream_health = "simulated"

        # Check for warnings: "running but doing nothing"
        warnings = []
        if self.is_running:
            if self.stats["last_report_time"] > 0 and (now - self.stats["last_report_time"]) > 900:
                warnings.append("WARNING: No agent reports received for > 15 minutes")
            if self.stats["last_order_time"] > 0 and (now - self.stats["last_order_time"]) > 14400:
                warnings.append(
                    "WARNING: Running but doing nothing (no orders placed for > 4 hours)"
                )

        status_data = {
            "is_running": self.is_running,
            "trading_mode": self.trading_mode,
            "approval_mode": self.approval_mode,
            "strategy": self.strategy.name,
            "breaker_state": self.breakers.halt_state,
            "kill_switch_halted": self.kill_switch.is_halted,
            "stream_health": stream_health,
            "balance": {
                "equity": str(balance.total_equity),
                "available": str(balance.available_balance),
                "unrealized_pnl": str(balance.unrealized_pnl),
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "qty": str(p.qty),
                    "entry_price": str(p.entry_price),
                    "mark_price": str(p.mark_price),
                    "unrealized_pnl": str(p.unrealized_pnl),
                    "stop_loss": str(p.stop_loss_price) if p.stop_loss_price else None,
                    "take_profit": str(p.take_profit_price) if p.take_profit_price else None,
                    "leverage": p.leverage,
                }
                for p in pos_list
                if p.qty > Decimal("0")
            ],
            "open_orders": [
                {
                    "symbol": o.symbol,
                    "order_id": o.order_id,
                    "client_order_id": o.client_order_id,
                    "side": o.side,
                    "order_type": o.order_type,
                    "price": str(o.price),
                    "qty": str(o.qty),
                    "stop_price": str(o.stop_price) if o.stop_price else None,
                }
                for o in open_orders
            ],
            "stats_24h": {
                "intents_count": self.stats["intents_count"],
                "approvals_count": self.stats["approvals_count"],
                "orders_sent": self.stats["orders_sent"],
                "orders_filled": self.stats["orders_filled"],
                "orders_rejected": self.stats["orders_rejected"],
                "reconciliation_fixes": self.stats["reconciliation_fixes"],
                "time_since_last_order_s": round(now - self.stats["last_order_time"], 1)
                if self.stats["last_order_time"] > 0
                else None,
                "time_since_last_report_s": round(now - self.stats["last_report_time"], 1)
                if self.stats["last_report_time"] > 0
                else None,
                "time_since_last_reconcile_s": round(now - self.stats["last_reconcile_time"], 1)
                if self.stats["last_reconcile_time"] > 0
                else None,
            },
            "warnings": warnings,
            "updated_at": now,
        }

        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.status_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=2)
        os.replace(temp_file, self.status_file)
