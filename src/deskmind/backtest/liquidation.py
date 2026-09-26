"""Liquidation calculations for Binance USDT-M perpetuals using maintenance margin brackets.

Checked against MARK price klines (high/low), not the last trade price.
A liquidation closes the position with the full margin loss + exchange liquidation fee.
"""

from __future__ import annotations

from decimal import Decimal

from deskmind.config import DEFAULT_MAINTENANCE_BRACKETS


def get_mmr(notional: Decimal, brackets: list[tuple[Decimal, Decimal]] | None = None) -> Decimal:
    """Return the maintenance margin rate for the given position notional."""
    b = brackets or DEFAULT_MAINTENANCE_BRACKETS
    for max_notional, mmr in b:
        if notional <= max_notional:
            return mmr
    return b[-1][1]


def compute_liquidation_price(
    side: str,
    entry_price: Decimal,
    leverage: int,
    qty: Decimal,
    brackets: list[tuple[Decimal, Decimal]] | None = None,
) -> Decimal:
    """Compute conservative liquidation price for isolated perpetual position."""
    if entry_price <= Decimal("0") or leverage <= 0 or qty <= Decimal("0"):
        return Decimal("0")

    notional = entry_price * qty
    mmr = get_mmr(notional, brackets)
    inv_l = Decimal("1") / Decimal(str(leverage))

    if side == "long":
        # P_liq = entry * (1 - 1/L + mmr) / (1 - mmr)
        denom = Decimal("1") - mmr
        if denom <= Decimal("0"):
            return Decimal("0")
        liq = entry_price * (Decimal("1") - inv_l + mmr) / denom
        return max(Decimal("0"), liq)
    else:
        # P_liq = entry * (1 + 1/L - mmr) / (1 + mmr)
        denom = Decimal("1") + mmr
        liq = entry_price * (Decimal("1") + inv_l - mmr) / denom
        return max(Decimal("0"), liq)


def is_liquidated(
    side: str,
    liquidation_price: Decimal,
    mark_high: Decimal,
    mark_low: Decimal,
) -> bool:
    """Check if the 5m mark price bar crossed the liquidation price."""
    if liquidation_price <= Decimal("0"):
        return False
    if side == "long":
        return mark_low <= liquidation_price
    else:
        return mark_high >= liquidation_price
