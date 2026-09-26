"""Sizing adapter integrating RiskManager with the PositionSizer protocol."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from deskmind.backtest.filters import SymbolFilters
from deskmind.risk.breakers import CircuitBreakerTracker
from deskmind.risk.config import RiskConfig
from deskmind.risk.evaluator import evaluate
from deskmind.risk.models import (
    IntentPayload,
    MarketState,
    RiskDecision,
)
from deskmind.strategies.base import AccountSnapshot, Intent


class RiskManagerSizer:
    """Position sizer backed by the deterministic Risk Manager."""

    def __init__(
        self,
        config: RiskConfig | None = None,
        tracker: CircuitBreakerTracker | None = None,
    ) -> None:
        self.config = config or RiskConfig()
        self.tracker = tracker or CircuitBreakerTracker(config=self.config)
        self.last_decision: RiskDecision | None = None

    def size_position(
        self,
        account: AccountSnapshot,
        intent: Intent,
        entry_price: Decimal,
        filters: SymbolFilters,
        *,
        mark_price: Decimal | None = None,
        atr_by_tf: dict[str, Decimal] | None = None,
        next_funding_time_ms: int | None = None,
        current_funding_rate: Decimal | None = None,
        timestamp_ms: int = 0,
        is_live: bool = False,
    ) -> tuple[Decimal, str]:
        """Evaluate intent through Risk Manager and return (qty, reason)."""
        side: Literal["long", "short"] = "long" if intent.action == "open_long" else "short"

        # Update circuit breaker tracker with current time & equity
        if timestamp_ms > 0:
            self.tracker.on_time_and_equity(timestamp_ms, account.equity)

        # Build account state from tracker & snapshot
        open_positions = [account.open_position.symbol] if account.open_position else []
        account_state = self.tracker.get_account_state(
            equity=account.equity,
            available_balance=account.cash,
            open_positions=open_positions,
        )

        # Build market state
        mkt_mark = mark_price or entry_price
        mkt_atr = atr_by_tf or {}
        market_state = MarketState(
            mark_price=mkt_mark,
            atr_by_tf=mkt_atr,
            next_funding_time_ms=next_funding_time_ms,
            current_funding_rate=current_funding_rate,
            filters=filters,
            is_live=is_live,
        )

        # Build intent payload
        intent_payload = IntentPayload(
            symbol=intent.symbol,
            side=side,
            expected_entry=entry_price,
            stop_loss=intent.stop_loss or Decimal("0"),
            take_profit=intent.take_profit or Decimal("0"),
            strategy_name=intent.metadata.get("strategy_name", "unknown"),
            strategy_version=intent.metadata.get("strategy_version", "1.0.0"),
            entry_timeframe=intent.metadata.get("entry_timeframe", "15m"),
            timestamp_ms=timestamp_ms,
        )

        # Evaluate through pure function
        decision = evaluate(
            intent=intent_payload,
            account=account_state,
            market=market_state,
            config=self.config,
        )
        self.last_decision = decision

        if decision.status == "approved":
            return decision.qty, "OK"
        else:
            first_fail = (
                decision.failed_reasons[0].message
                if decision.failed_reasons
                else "risk manager rejected"
            )
            return Decimal("0"), f"risk manager rejected: {first_fail}"
