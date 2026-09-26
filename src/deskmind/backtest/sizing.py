"""Position sizing interface for backtesting.

All sizing math must use Decimal.
Default: fixed-fractional sizer risking X% of equity per trade (default 1%).
Step 6 Risk Manager will replace it through this exact same interface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from deskmind.backtest.filters import SymbolFilters, round_qty, validate_order
from deskmind.strategies.base import AccountSnapshot, Intent


class PositionSizer(Protocol):
    def size_position(
        self,
        account: AccountSnapshot,
        intent: Intent,
        entry_price: Decimal,
        filters: SymbolFilters,
    ) -> tuple[Decimal, str]:
        """Calculate position quantity in contract units.

        Returns (qty, reason). If qty is 0, reason explains why (e.g. filter rejection,
        insufficient cash).
        """
        ...


class FixedFractionalSizer:
    def __init__(
        self,
        risk_pct: Decimal = Decimal("0.01"),  # 1% equity risk
        max_leverage: int = 5,
    ) -> None:
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage

    def size_position(
        self,
        account: AccountSnapshot,
        intent: Intent,
        entry_price: Decimal,
        filters: SymbolFilters,
    ) -> tuple[Decimal, str]:
        if account.equity <= Decimal("0"):
            return Decimal("0"), "zero or negative equity"

        if intent.stop_loss is None:
            return Decimal("0"), "no stop loss specified"

        price_risk = abs(entry_price - intent.stop_loss)
        if price_risk <= Decimal("0"):
            return Decimal("0"), "stop loss equals entry price"

        # Dollar risk = equity * risk_pct
        risk_cash = account.equity * self.risk_pct

        # Raw qty based on risk distance
        raw_qty = risk_cash / price_risk

        # Leverage cap: notional <= equity * max_leverage
        max_notional = account.equity * Decimal(str(self.max_leverage))
        max_qty_by_leverage = (
            max_notional / entry_price if entry_price > Decimal("0") else Decimal("0")
        )
        qty = min(raw_qty, max_qty_by_leverage)

        # Round down to exchange stepSize
        rounded_qty = round_qty(qty, filters.step_size)

        # Validate against exchange filters
        valid, msg = validate_order(entry_price, rounded_qty, filters)
        if not valid:
            return Decimal("0"), f"exchange filter rejected: {msg}"

        return rounded_qty, "OK"
