"""Approval Gate for live/testnet trade execution.

Manages manual and automatic approval workflows for trade intents.
In 'manual' mode, intents must be approved via CLI within 10 minutes,
and are strictly re-checked by the Risk Manager upon approval.
In 'auto' mode (allowed only on paper/testnet), intents pass directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from deskmind.risk.config import RiskConfig
from deskmind.risk.evaluator import evaluate
from deskmind.risk.models import AccountState, IntentPayload, MarketState, RiskDecision

logger = logging.getLogger(__name__)

DEFAULT_APPROVAL_DIR = Path("data/trade")
PENDING_FILE = DEFAULT_APPROVAL_DIR / "pending_approvals.json"
EXPIRY_SECONDS = 600.0  # 10 minutes


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


@dataclass
class ApprovalRequest:
    id: str
    intent: IntentPayload
    decision: RiskDecision
    account: AccountState
    market: MarketState
    status: str  # "pending", "approved", "rejected", "expired"
    created_at: float
    expires_at: float
    resolved_at: float | None = None
    resolution_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intent": {
                "symbol": self.intent.symbol,
                "side": self.intent.side,
                "expected_entry": str(self.intent.expected_entry),
                "stop_loss": str(self.intent.stop_loss),
                "take_profit": str(self.intent.take_profit),
                "strategy_name": self.intent.strategy_name,
                "strategy_version": self.intent.strategy_version,
                "setup_id": self.intent.setup_id,
            },
            "decision": {
                "approved": self.decision.approved,
                "leverage": self.decision.leverage,
                "qty": str(self.decision.qty),
                "notional": str(self.decision.notional),
                "liquidation_price": str(self.decision.liquidation_price),
            },
            "account": {
                "equity": str(self.account.equity),
                "available_balance": str(self.account.available_balance),
            },
            "market": {
                "mark_price": str(self.market.mark_price),
                "atr": str(self.market.atr_by_tf.get("15m", "1")),
            },
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resolved_at": self.resolved_at,
            "resolution_note": self.resolution_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRequest:
        intent_d = data["intent"]
        intent = IntentPayload(
            symbol=intent_d["symbol"],
            side=intent_d["side"],
            expected_entry=Decimal(intent_d["expected_entry"]),
            stop_loss=Decimal(intent_d["stop_loss"]),
            take_profit=Decimal(intent_d["take_profit"]),
            strategy_name=intent_d.get("strategy_name", "unknown"),
            strategy_version=intent_d.get("strategy_version", "v1"),
            setup_id=intent_d.get("setup_id", ""),
        )
        dec_d = data["decision"]
        decision = RiskDecision(
            status="approved" if dec_d.get("approved") else "rejected",
            leverage=dec_d.get("leverage", 1),
            qty=Decimal(dec_d.get("qty", "0")),
            notional=Decimal(dec_d.get("notional", "0")),
            liquidation_price=Decimal(dec_d.get("liquidation_price", "0")),
            reasons=[],
        )
        acc_d = data.get("account", {})
        account = AccountState(
            equity=Decimal(acc_d.get("equity", "10000")),
            available_balance=Decimal(acc_d.get("available_balance", "10000")),
        )
        mkt_d = data.get("market", {})
        market = MarketState(
            mark_price=Decimal(mkt_d.get("mark_price", str(intent.expected_entry))),
            atr_by_tf={"15m": Decimal(mkt_d.get("atr", "1"))},
        )
        return cls(
            id=data["id"],
            intent=intent,
            decision=decision,
            account=account,
            market=market,
            status=data["status"],
            created_at=data["created_at"],
            expires_at=data["expires_at"],
            resolved_at=data.get("resolved_at"),
            resolution_note=data.get("resolution_note"),
        )


class ApprovalGate:
    """Manages trade intent approvals."""

    def __init__(
        self,
        mode: str = "auto",
        trading_mode: str = "paper",
        storage_path: Path = PENDING_FILE,
        risk_config: RiskConfig | None = None,
    ) -> None:
        self.mode = mode.lower()
        self.trading_mode = trading_mode.lower()
        self.storage_path = storage_path
        self.risk_config = risk_config or RiskConfig()

        if self.mode == "auto" and self.trading_mode not in ("paper", "testnet"):
            raise ValueError(
                f"APPROVAL_MODE 'auto' is forbidden in '{self.trading_mode}' mode. Only 'paper' or 'testnet' allowed."
            )

        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._requests: dict[str, ApprovalRequest] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, encoding="utf-8") as f:
                data = json.load(f)
                for req_d in data.values():
                    req = ApprovalRequest.from_dict(req_d)
                    self._requests[req.id] = req
        except Exception as e:
            logger.warning("Failed to load pending approvals from %s: %s", self.storage_path, e)

    def _save_to_disk(self) -> None:
        try:
            temp_path = self.storage_path.with_suffix(".tmp")
            data = {req_id: req.to_dict() for req_id, req in self._requests.items()}
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, cls=DecimalEncoder)
            os.replace(temp_path, self.storage_path)
        except Exception as e:
            logger.error("Failed to save pending approvals to %s: %s", self.storage_path, e)

    def request_approval(
        self,
        intent: IntentPayload,
        decision: RiskDecision,
        account: AccountState,
        market: MarketState,
    ) -> tuple[bool, str, ApprovalRequest]:
        """Submit a trade intent for approval.

        Returns (is_approved_immediately, request_id, request_object).
        """
        now = time.time()
        req_id = f"req_{int(now)}_{intent.symbol}_{intent.side}"

        req = ApprovalRequest(
            id=req_id,
            intent=intent,
            decision=decision,
            account=account,
            market=market,
            status="pending",
            created_at=now,
            expires_at=now + EXPIRY_SECONDS,
        )

        if self.mode == "auto":
            req.status = "approved"
            req.resolved_at = now
            req.resolution_note = "Auto-approved in auto mode"
            self._requests[req.id] = req
            self._save_to_disk()
            logger.info("Auto-approved trade %s for %s %s", req.id, intent.symbol, intent.side)
            return True, req.id, req

        self._requests[req.id] = req
        self._save_to_disk()
        logger.info(
            "Held trade %s for MANUAL approval. Expires in 10 minutes. Run: uv run deskmind trade approve %s",
            req.id,
            req.id,
        )
        return False, req.id, req

    def get_pending(self) -> list[ApprovalRequest]:
        """Return list of currently pending, non-expired approval requests."""
        self._load_from_disk()
        now = time.time()
        pending = []
        for req in self._requests.values():
            if req.status == "pending":
                if now > req.expires_at:
                    req.status = "expired"
                    req.resolved_at = now
                    req.resolution_note = "Expired after 10 minutes without approval"
                else:
                    pending.append(req)
        self._save_to_disk()
        return pending

    def approve(self, req_id: str, note: str = "Manual approval via CLI") -> bool:
        """Approve a pending request."""
        self._load_from_disk()
        req = self._requests.get(req_id)
        if not req:
            logger.error("Approval request %s not found", req_id)
            return False
        if req.status != "pending":
            logger.warning(
                "Request %s cannot be approved: current status is %s", req_id, req.status
            )
            return False

        now = time.time()
        if now > req.expires_at:
            req.status = "expired"
            req.resolved_at = now
            req.resolution_note = "Expired after 10 minutes"
            self._save_to_disk()
            logger.warning("Request %s has expired and cannot be approved", req_id)
            return False

        req.status = "approved"
        req.resolved_at = now
        req.resolution_note = note
        self._save_to_disk()
        logger.info("Approved trade request %s", req_id)
        return True

    def reject(self, req_id: str, note: str = "Manual rejection via CLI") -> bool:
        """Reject a pending request."""
        self._load_from_disk()
        req = self._requests.get(req_id)
        if not req:
            logger.error("Approval request %s not found", req_id)
            return False
        if req.status != "pending":
            logger.warning(
                "Request %s cannot be rejected: current status is %s", req_id, req.status
            )
            return False

        now = time.time()
        req.status = "rejected"
        req.resolved_at = now
        req.resolution_note = note
        self._save_to_disk()
        logger.info("Rejected trade request %s", req_id)
        return True

    def process_and_revalidate(
        self,
        req_id: str,
        current_account: AccountState,
        current_market: MarketState,
    ) -> tuple[bool, RiskDecision | None, str]:
        """Re-check an approved request with Risk Manager before live execution.

        Returns (can_execute, new_decision_or_none, reason).
        """
        self._load_from_disk()
        req = self._requests.get(req_id)
        if not req:
            return False, None, f"Request {req_id} not found"

        now = time.time()
        if req.status != "approved":
            return False, None, f"Request status is {req.status}, not approved"

        if now > req.expires_at:
            req.status = "expired"
            self._save_to_disk()
            return False, None, f"Request {req_id} expired before execution"

        # Re-check via Risk Manager with current account and market states
        new_decision = evaluate(
            intent=req.intent,
            account=current_account,
            market=current_market,
            config=self.risk_config,
        )

        if not new_decision.approved:
            failing = [r.message for r in new_decision.reasons if not r.passed]
            reason_str = "; ".join(failing)
            logger.warning(
                "Trade %s approved by user but RE-REJECTED by Risk Manager: %s",
                req_id,
                reason_str,
            )
            req.status = "rejected"
            req.resolution_note = f"Risk Manager re-check failed: {reason_str}"
            self._save_to_disk()
            return False, new_decision, f"Risk Manager re-check failed: {reason_str}"

        return True, new_decision, "Passed Risk Manager re-check"
