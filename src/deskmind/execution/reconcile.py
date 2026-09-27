"""Periodic and startup state reconciliation between DB, memory, and exchange."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from deskmind.db.models import TradeJournal
from deskmind.db.session import session_scope
from deskmind.logging import get_logger

if TYPE_CHECKING:
    from deskmind.execution.executor import BaseExecutor
    from deskmind.execution.journal import TradeJournalManager

log = get_logger(__name__)


class Reconciler:
    """Reconciles internal state with live exchange state on startup and every 60 seconds.

    Handles:
    - Position without stop: place stop or flatten immediately.
    - Orphan orders: cancel.
    - DB open but exchange closed: close in journal and cancel lingering orders.
    - Unknown positions: alert and flatten.
    - Mid-position restart: recover state with no duplicate orders.
    """

    def __init__(
        self,
        executor: BaseExecutor,
        journal: TradeJournalManager,
    ) -> None:
        self.executor = executor
        self.journal = journal
        self.reconciliation_count = 0
        self.fixes_applied_count = 0
        self.positions_without_stop_detected = 0

    async def reconcile(self) -> dict[str, Any]:
        """Execute one reconciliation pass."""
        self.reconciliation_count += 1
        now_ms = int(time.time() * 1000)
        report: dict[str, Any] = {
            "timestamp_ms": now_ms,
            "actions": [],
            "status": "clean",
        }

        try:
            exchange_positions = await self.executor.get_positions()
            open_orders = await self.executor.get_open_orders()
            db_open_trades = await self.journal.get_open_trades()

            orders_by_sym: dict[str, list[Any]] = {}
            for o in open_orders:
                orders_by_sym.setdefault(o.symbol, []).append(o)

            # Case 1: Position without stop
            for sym, pos in exchange_positions.items():
                if pos.is_open and not pos.has_active_stop:
                    self.positions_without_stop_detected += 1
                    log.critical(
                        "RECONCILIATION_POSITION_WITHOUT_STOP",
                        symbol=sym,
                        qty=str(pos.qty),
                        msg="Position lacks active stop-loss order!",
                    )
                    # Attempt to place stop or flatten immediately
                    if pos.stop_loss_price is not None and pos.stop_loss_price > Decimal("0"):
                        move_res = await self.executor.move_stop(sym, pos.stop_loss_price)
                        if move_res.success:
                            report["actions"].append(
                                f"Placed missing stop for {sym} at {pos.stop_loss_price}"
                            )
                            self.fixes_applied_count += 1
                            continue

                    # If no valid stop or placement failed -> flatten immediately
                    log.warning("flattening_unprotected_position", symbol=sym)
                    await self.executor.close(sym, reason="reconcile_no_stop_flatten")
                    report["actions"].append(f"Flattened unprotected position for {sym}")
                    self.fixes_applied_count += 1

            # Case 2: Orphan orders (orders for a flat symbol)
            for sym, sym_orders in orders_by_sym.items():
                pos = exchange_positions.get(sym)
                if pos is None or not pos.is_open:
                    for o in sym_orders:
                        log.warning(
                            "canceling_orphan_order",
                            symbol=sym,
                            cid=o.client_order_id,
                            type=o.order_type,
                        )
                    await self.executor.cancel_all(sym)
                    report["actions"].append(
                        f"Canceled {len(sym_orders)} orphan orders for flat symbol {sym}"
                    )
                    self.fixes_applied_count += 1

            # Case 3: DB open but exchange closed
            for trade in db_open_trades:
                sym = trade.get("symbol", "")
                cid = trade.get("client_order_id", "")
                pos = exchange_positions.get(sym)
                if pos is None or not pos.is_open:
                    log.info("reconcile_db_open_exchange_closed", symbol=sym, cid=cid)
                    # Close in journal and cancel any lingering opposite orders
                    await self.executor.cancel_all(sym)
                    await self.journal.record_exit(
                        symbol=sym,
                        client_order_id=cid,
                        exit_price=trade.get("entry", Decimal("0")),
                        pnl=Decimal("0"),
                        reason="reconcile_exchange_closed",
                    )
                    report["actions"].append(
                        f"Synchronized closed trade in journal for {sym} ({cid})"
                    )
                    self.fixes_applied_count += 1

            # Case 4: Unknown position on exchange (not in DB)
            db_symbols = {t.get("symbol") for t in db_open_trades}
            for sym, pos in exchange_positions.items():
                if pos.is_open and sym not in db_symbols:
                    log.critical("RECONCILIATION_UNKNOWN_POSITION", symbol=sym, qty=str(pos.qty))
                    await self.executor.close(sym, reason="reconcile_unknown_position")
                    report["actions"].append(f"Flattened unknown position for {sym}")
                    self.fixes_applied_count += 1

            if report["actions"]:
                report["status"] = "fixed"
            return report

        except Exception as e:
            log.error("reconciliation_error", error=str(e))
            report["status"] = "error"
            report["error"] = str(e)
            return report

    async def _get_db_open_trades(self) -> list[dict[str, Any]]:
        """Query DB for trades with status='open'."""
        try:
            async with session_scope() as session:
                stmt = select(TradeJournal).where(TradeJournal.status == "open")
                res = await session.execute(stmt)
                trades = res.scalars().all()
                return [
                    {
                        "symbol": t.symbol,
                        "client_order_id": t.client_order_id,
                        "entry": t.entry or Decimal("0"),
                        "side": t.side,
                        "sl": t.sl,
                        "tp": t.tp,
                    }
                    for t in trades
                ]
        except Exception as e:
            log.debug("reconcile_db_query_fallback", error=str(e))
            return []
