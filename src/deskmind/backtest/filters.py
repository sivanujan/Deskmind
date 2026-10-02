"""Exchange filters and rounding for Binance USDT-M perpetuals.

Money and size calculations MUST use Decimal, never float.
Filters are loaded from the exchangeInfo snapshots in data/meta/{symbol}_filters_{date}.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    price_precision: int
    quantity_precision: int
    liquidation_fee: Decimal = Decimal("0.0125")  # default 1.25%


KNOWN_BINANCE_FILTERS: dict[str, dict[str, Any]] = {
    "BTCUSDT": {"tick_size": Decimal("0.10"), "step_size": Decimal("0.001"), "min_qty": Decimal("0.001"), "max_qty": Decimal("1000.0"), "price_prec": 1, "qty_prec": 3},
    "ETHUSDT": {"tick_size": Decimal("0.01"), "step_size": Decimal("0.001"), "min_qty": Decimal("0.001"), "max_qty": Decimal("10000.0"), "price_prec": 2, "qty_prec": 3},
    "BNBUSDT": {"tick_size": Decimal("0.01"), "step_size": Decimal("0.01"), "min_qty": Decimal("0.01"), "max_qty": Decimal("10000.0"), "price_prec": 2, "qty_prec": 2},
    "SOLUSDT": {"tick_size": Decimal("0.01"), "step_size": Decimal("0.01"), "min_qty": Decimal("0.01"), "max_qty": Decimal("100000.0"), "price_prec": 2, "qty_prec": 2},
    "XRPUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1"), "max_qty": Decimal("10000000.0"), "price_prec": 4, "qty_prec": 1},
    "DOGEUSDT": {"tick_size": Decimal("0.00001"), "step_size": Decimal("1.0"), "min_qty": Decimal("1.0"), "max_qty": Decimal("10000000.0"), "price_prec": 5, "qty_prec": 0},
    "ADAUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("1.0"), "min_qty": Decimal("1.0"), "max_qty": Decimal("10000000.0"), "price_prec": 4, "qty_prec": 0},
    "MATICUSDT": {"tick_size": Decimal("0.0001"), "step_size": Decimal("0.1"), "min_qty": Decimal("0.1"), "max_qty": Decimal("10000000.0"), "price_prec": 4, "qty_prec": 1},
    "LINKUSDT": {"tick_size": Decimal("0.001"), "step_size": Decimal("0.01"), "min_qty": Decimal("0.01"), "max_qty": Decimal("1000000.0"), "price_prec": 3, "qty_prec": 2},
    "LTCUSDT": {"tick_size": Decimal("0.01"), "step_size": Decimal("0.001"), "min_qty": Decimal("0.001"), "max_qty": Decimal("100000.0"), "price_prec": 2, "qty_prec": 3},
}


def load_symbol_filters(data_dir: Path, symbol: str) -> SymbolFilters:
    """Load latest exchange filter snapshot for symbol from meta directory, with known fallback."""
    meta_dir = data_dir / "meta"
    matches = sorted(meta_dir.glob(f"{symbol}_filters_*.json"))
    if not matches:
        kb = KNOWN_BINANCE_FILTERS.get(symbol, {})
        return SymbolFilters(
            symbol=symbol,
            tick_size=kb.get("tick_size", Decimal("0.01")),
            step_size=kb.get("step_size", Decimal("0.001")),
            min_qty=kb.get("min_qty", Decimal("0.001")),
            max_qty=kb.get("max_qty", Decimal("10000.0")),
            min_notional=Decimal("5.0"),
            price_precision=kb.get("price_prec", 2),
            quantity_precision=kb.get("qty_prec", 3),
            liquidation_fee=Decimal("0.0125"),
        )
    latest_file = matches[-1]
    with open(latest_file, encoding="utf-8") as f:
        data = json.load(f)

    summary = data.get("summary", {})
    contract = data.get("contract", {})

    return SymbolFilters(
        symbol=symbol,
        tick_size=Decimal(str(summary.get("tickSize", "0.10"))),
        step_size=Decimal(str(summary.get("stepSize", "0.001"))),
        min_qty=Decimal(str(summary.get("minQty", "0.001"))),
        max_qty=Decimal(str(summary.get("maxQty", "1000.0"))),
        min_notional=Decimal(str(summary.get("minNotional", "5.0"))),
        price_precision=int(summary.get("pricePrecision", 2)),
        quantity_precision=int(summary.get("quantityPrecision", 3)),
        liquidation_fee=Decimal(str(contract.get("liquidationFee", "0.0125"))),
    )


def round_price(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round price to the nearest tickSize."""
    if tick_size <= Decimal("0"):
        return price
    return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick_size


def round_qty(qty: Decimal, step_size: Decimal) -> Decimal:
    """Round qty down to stepSize (conservative to not exceed margin)."""
    if step_size <= Decimal("0"):
        return qty
    return (qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size


def validate_order(price: Decimal, qty: Decimal, filters: SymbolFilters) -> tuple[bool, str]:
    """Validate order against exchange filters (minQty, maxQty, minNotional)."""
    if qty < filters.min_qty:
        return False, f"qty {qty} < minQty {filters.min_qty}"
    if qty > filters.max_qty:
        return False, f"qty {qty} > maxQty {filters.max_qty}"
    notional = price * qty
    if notional < filters.min_notional:
        return False, f"notional {notional:.2f} < minNotional {filters.min_notional}"
    return True, "OK"
