"""Trade journal manager for database and fallback JSONL logging of execution lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from deskmind.db.models import TradeJournal
from deskmind.db.session import session_scope
from deskmind.execution.models import OrderInfo
from deskmind.logging import get_logger

log = get_logger(__name__)


class TradeJournalManager:
    """Manages trade audit journal in PostgreSQL with graceful local JSONL fallback."""

    def __init__(
        self,
        journal_file: Path | None = None,
        trading_mode: str = "paper",
    ) -> None:
        self.trading_mode = trading_mode.lower()
        self.journal_file = journal_file or Path("data/journal/trade_journal.jsonl")
        self.journal_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlays_file = Path("data/journal/exec_overlays.json")
        self.overlays_file.parent.mkdir(parents=True, exist_ok=True)

    def _write_fallback(self, record: dict[str, Any]) -> None:
        """Write record to local JSONL file."""
        try:
            with open(self.journal_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            log.error("journal_fallback_write_error", error=str(e))

    async def record_intent(self, intent: Any, decision: Any) -> None:
        """Log intent and risk decision."""
        rec = {
            "type": "intent",
            "symbol": intent.symbol,
            "side": intent.side,
            "expected_entry": str(intent.expected_entry),
            "stop_loss": str(intent.stop_loss),
            "take_profit": str(intent.take_profit),
            "decision": decision.to_dict() if hasattr(decision, "to_dict") else str(decision),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._write_fallback(rec)

    async def record_order(self, order: OrderInfo) -> None:
        """Log order placement or update."""
        rec = {
            "type": "order",
            "client_order_id": order.client_order_id,
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side,
            "order_type": order.order_type,
            "qty": str(order.qty),
            "price": str(order.price),
            "stop_price": str(order.stop_price) if order.stop_price else None,
            "status": order.status,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._write_fallback(rec)

    async def get_open_trades(self) -> list[dict[str, Any]]:
        """Query DB for trades with status='open' with local JSONL fallback."""
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
        except Exception:
            pass

        # Fallback to local JSONL
        if not self.journal_file.exists():
            return []
        open_by_cid: dict[str, dict[str, Any]] = {}
        try:
            with open(self.journal_file, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    cid = item.get("client_order_id")
                    if not cid:
                        continue
                    if item.get("status") == "open":
                        open_by_cid[cid] = item
                    elif item.get("status") == "closed" and cid in open_by_cid:
                        del open_by_cid[cid]
        except Exception:
            return []
        return list(open_by_cid.values())

    async def record_entry(
        self,
        symbol: str,
        side: str,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        qty: Decimal,
        leverage: int,
        client_order_id: str,
        risk_decision: dict[str, Any],
        source: str = "exec:testnet",  # exec:testnet or exec:paper
    ) -> None:
        """Record trade opening in trade_journal table and local fallback."""
        now_dt = datetime.now(UTC)
        record = {
            "created_at": now_dt.isoformat(),
            "symbol": symbol,
            "side": side,
            "entry": str(entry_price),
            "sl": str(stop_loss),
            "tp": str(take_profit),
            "size": str(qty),
            "leverage": leverage,
            "status": "open",
            "pnl": "0.0",
            "head_agent_reasoning": None,
            "risk_decision": risk_decision,
            "client_order_id": client_order_id,
            "source": source,
        }
        self._write_fallback(record)
        self._record_overlay_marker(
            symbol=symbol,
            time_s=int(now_dt.timestamp()),
            price=float(entry_price),
            side=side,
            kind="entry",
            source_agent=source,
            text=f"{side.upper()} {qty} @ {entry_price}",
        )

        try:
            async with session_scope() as session:
                entry = TradeJournal(
                    created_at=now_dt,
                    symbol=symbol,
                    side=side,
                    entry=entry_price,
                    sl=stop_loss,
                    tp=take_profit,
                    size=qty,
                    leverage=leverage,
                    status="open",
                    pnl=Decimal("0"),
                    head_agent_reasoning=None,
                    risk_decision=risk_decision,
                    client_order_id=client_order_id,
                    trading_mode=self.trading_mode,
                    fees_paid=Decimal("0"),
                )
                session.add(entry)
            log.info("trade_journal_entry_recorded", symbol=symbol, cid=client_order_id)
        except Exception as e:
            log.warning(
                "trade_journal_db_unavailable", note="Persisted to JSONL fallback", error=str(e)
            )

    async def record_exit(
        self,
        symbol: str,
        client_order_id: str,
        exit_price: Decimal,
        pnl: Decimal,
        reason: str = "close",
        fees: Decimal = Decimal("0"),
        funding: Decimal = Decimal("0"),
        source: str = "exec:testnet",
    ) -> None:
        """Update trade_journal record on exit."""
        now_dt = datetime.now(UTC)
        record = {
            "exit_at": now_dt.isoformat(),
            "symbol": symbol,
            "client_order_id": client_order_id,
            "exit_price": str(exit_price),
            "pnl": str(pnl),
            "reason": reason,
            "fees": str(fees),
            "funding": str(funding),
            "status": "closed",
            "source": source,
        }
        self._write_fallback(record)
        self._record_overlay_marker(
            symbol=symbol,
            time_s=int(now_dt.timestamp()),
            price=float(exit_price),
            side="exit",
            kind="exit",
            source_agent=source,
            text=f"EXIT ({reason}) PnL: ${pnl:.2f}",
        )

        try:
            async with session_scope() as session:
                stmt = select(TradeJournal).where(TradeJournal.client_order_id == client_order_id)
                res = await session.execute(stmt)
                tj = res.scalar_one_or_none()
                if tj is not None:
                    tj.status = "closed"
                    tj.pnl = pnl
                    tj.exit_price = exit_price
                    tj.exit_time = now_dt
                    tj.exit_reason = reason
                    tj.fees_paid = fees
                    if tj.risk_decision:
                        dec = dict(tj.risk_decision)
                        dec["exit"] = {
                            "exit_price": str(exit_price),
                            "exit_reason": reason,
                            "fees": str(fees),
                            "funding": str(funding),
                            "exit_time": now_dt.isoformat(),
                        }
                        tj.risk_decision = dec
            log.info(
                "trade_journal_exit_recorded", symbol=symbol, cid=client_order_id, pnl=str(pnl)
            )
        except Exception as e:
            log.warning(
                "trade_journal_db_exit_failed", note="Persisted to JSONL fallback", error=str(e)
            )

    def _record_overlay_marker(
        self,
        symbol: str,
        time_s: int,
        price: float,
        side: str,
        kind: str,
        source_agent: str,
        text: str,
    ) -> None:
        """Store chart overlay for executed trade with source_agent exec:testnet / exec:paper."""
        shape = (
            "arrowUp"
            if side.lower() == "long"
            else ("arrowDown" if side.lower() == "short" else "circle")
        )
        color = (
            "#22c55e"
            if side.lower() == "long"
            else ("#ef4444" if side.lower() == "short" else "#38bdf8")
        )

        overlay = {
            "id": f"exec_{symbol}_{time_s}_{kind}",
            "type": "marker",
            "kind": f"trade_{kind}",
            "source_agent": source_agent,
            "agent_version": "1.0.0",
            "schema_version": 1,
            "time": time_s,
            "position": "below" if side.lower() == "long" else "above",
            "shape": shape,
            "color": color,
            "text": text,
            "meta": {"symbol": symbol, "price": price},
        }

        try:
            existing = []
            if self.overlays_file.exists():
                with open(self.overlays_file, encoding="utf-8") as f:
                    existing = json.load(f)
            existing.append(overlay)
            with open(self.overlays_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            log.debug("overlay_record_error", error=str(e))
