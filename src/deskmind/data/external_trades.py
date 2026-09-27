"""Import the user's own Binance trade history into `external_trades` (read-only, never traded on).

Sources:
- API (read-only mainnet key, see data/mainnet_readonly.py):
  futures: /fapi/v1/income discovers traded symbols, /fapi/v1/userTrades fetches fills
  (Binance keeps ~3 months); spot: /api/v3/account balances -> candidate symbols,
  /api/v3/myTrades full history per symbol. If futures are refused, they are skipped with
  "Needs futures read access — not enabled (by design)".
- CSV exported from Binance (Orders -> Trade History -> Export), spot and USD-M futures, in the
  column layouts listed in CSV_ALIASES. Unknown layouts are rejected with the headers found.

Values are Decimal end to end. Dedupe: unique (source, market, symbol, trade_id); CSV rows
without an exchange trade id get a deterministic content hash so re-imports never duplicate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from deskmind.db.models import ExternalTrade
from deskmind.logging import get_logger

log = get_logger(__name__)

SPOT_QUOTES = ("USDT", "USDC", "FDUSD", "BTC")
DAY_MS = 86_400_000


@dataclass(frozen=True)
class TradeRow:
    source: str  # api | csv
    market: str  # spot | futures
    symbol: str
    side: str  # BUY | SELL
    price: Decimal
    qty: Decimal
    quote_qty: Decimal | None
    fee: Decimal | None
    fee_asset: str | None
    realized_pnl: Decimal | None
    time: datetime
    order_id: str | None
    trade_id: str

    def db_row(self) -> dict[str, Any]:
        return asdict(self)


def _ms(v: Any) -> datetime:
    return datetime.fromtimestamp(int(v) / 1000, UTC)


def _dec(v: Any) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- API rows


def from_futures_api(r: dict[str, Any]) -> TradeRow:
    return TradeRow(
        source="api",
        market="futures",
        symbol=r["symbol"],
        side=str(r["side"]).upper(),
        price=Decimal(str(r["price"])),
        qty=Decimal(str(r["qty"])),
        quote_qty=_dec(r.get("quoteQty")),
        fee=_dec(r.get("commission")),
        fee_asset=r.get("commissionAsset"),
        realized_pnl=_dec(r.get("realizedPnl")),
        time=_ms(r["time"]),
        order_id=str(r["orderId"]) if r.get("orderId") is not None else None,
        trade_id=str(r["id"]),
    )


def from_spot_api(r: dict[str, Any]) -> TradeRow:
    return TradeRow(
        source="api",
        market="spot",
        symbol=r["symbol"],
        side="BUY" if r.get("isBuyer") else "SELL",
        price=Decimal(str(r["price"])),
        qty=Decimal(str(r["qty"])),
        quote_qty=_dec(r.get("quoteQty")),
        fee=_dec(r.get("commission")),
        fee_asset=r.get("commissionAsset"),
        realized_pnl=None,  # spot fills have no realized PnL
        time=_ms(r["time"]),
        order_id=str(r["orderId"]) if r.get("orderId") is not None else None,
        trade_id=str(r["id"]),
    )


# ---------------------------------------------------------------- CSV exports

# Known Binance export column names (lower-cased) for each field.
CSV_ALIASES: dict[str, tuple[str, ...]] = {
    "time": ("date(utc)", "date(utc+0)", "time", "date", "time(utc)"),
    "symbol": ("pair", "market", "symbol"),
    "side": ("side", "type"),
    "price": ("price",),
    "qty": ("executed", "quantity", "qty", "amount_base"),
    "quote_qty": ("total", "quote quantity", "quote qty"),
    "fee": ("fee",),
    "fee_asset": ("fee coin", "fee asset", "commission asset"),
    "realized_pnl": ("realized profit", "realized pnl", "realized_pnl"),
    "trade_id": ("trade id", "tradeid", "id"),
    "order_id": ("order id", "order no.", "orderid"),
}
NUM_WITH_ASSET = re.compile(r"^\s*([-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?)\s*([A-Za-z0-9]*)\s*$")


class CsvFormatError(ValueError):
    pass


def _split_num_asset(v: str) -> tuple[Decimal | None, str | None]:
    """'0.00100000BTC' -> (0.001, 'BTC'); '12.5' -> (12.5, None)."""
    m = NUM_WITH_ASSET.match(v or "")
    if not m:
        return None, None
    return _dec(m.group(1)), (m.group(2).upper() or None)


def _parse_time(v: str) -> datetime:
    v = v.strip()
    if v.isdigit():
        return _ms(v if len(v) >= 13 else int(v) * 1000)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise CsvFormatError(f"unrecognised time value {v!r}")


def parse_trade_csv(text: str, market: str | None = None) -> tuple[str, list[TradeRow]]:
    """Parse a Binance trade-history export. Returns (market, rows).

    Old spot layout (Date(UTC),Pair,Side,Price,Executed,Amount,Fee) carries the asset inside the
    Executed / Amount / Fee values ("0.001BTC"); newer layouts use separate Total / Fee Coin
    columns. Futures exports have Quantity and Realized Profit.
    """
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    if not headers:
        raise CsvFormatError("empty file or no header row")
    col: dict[str, str] = {}
    for field_name, aliases in CSV_ALIASES.items():
        for a in aliases:
            if a in headers:
                col[field_name] = reader.fieldnames[headers.index(a)]  # type: ignore[index]
                break
    # "Amount" is the quote amount when another column holds the base quantity (old spot
    # "Executed", futures "Quantity"); in the newer spot layout it is the base quantity itself.
    if "amount" in headers:
        amount_col = reader.fieldnames[headers.index("amount")]  # type: ignore[index]
        if "qty" in col:
            col.setdefault("quote_qty", amount_col)
        else:
            col["qty"] = amount_col
    missing = [f for f in ("time", "symbol", "side", "price", "qty") if f not in col]
    if missing:
        raise CsvFormatError(
            f"unknown CSV layout: missing {missing}; headers found: {reader.fieldnames}"
        )
    if market is None:
        futures_hint = "realized_pnl" in col or "quantity" in headers
        market = "futures" if futures_hint else "spot"
    if market not in ("spot", "futures"):
        raise CsvFormatError(f"market must be spot or futures, got {market!r}")

    rows: list[TradeRow] = []
    for raw in reader:
        if not raw or not (raw.get(col["time"]) or "").strip():
            continue
        qty, _base = _split_num_asset(raw[col["qty"]])
        price = _dec(raw[col["price"]])
        if qty is None or price is None:
            raise CsvFormatError(f"bad price/qty in row: {raw}")
        quote, _quote_asset = (
            _split_num_asset(raw[col["quote_qty"]]) if "quote_qty" in col else (None, None)
        )
        fee, fee_asset = _split_num_asset(raw[col["fee"]]) if "fee" in col else (None, None)
        if "fee_asset" in col and raw.get(col["fee_asset"]):
            fee_asset = raw[col["fee_asset"]].strip().upper()
        symbol = raw[col["symbol"]].strip().upper().replace("/", "").replace("-", "")
        side = raw[col["side"]].strip().upper()
        when = _parse_time(raw[col["time"]])
        trade_id = (raw.get(col["trade_id"], "") or "").strip() if "trade_id" in col else ""
        if not trade_id:
            digest = hashlib.sha1(
                "|".join(str(raw[k]) for k in sorted(raw) if k is not None).encode()
            ).hexdigest()
            trade_id = f"csv-{digest}"
        rows.append(
            TradeRow(
                source="csv",
                market=market,
                symbol=symbol,
                side=side,
                price=price,
                qty=abs(qty),
                quote_qty=abs(quote) if quote is not None else None,
                fee=abs(fee) if fee is not None else None,
                fee_asset=fee_asset,
                realized_pnl=_dec(raw[col["realized_pnl"]]) if "realized_pnl" in col else None,
                time=when,
                order_id=((raw.get(col["order_id"]) or "").strip() or None)
                if "order_id" in col
                else None,
                trade_id=trade_id,
            )
        )
    return market, rows


# ---------------------------------------------------------------- storage


async def upsert_trades(session: Any, rows: list[TradeRow]) -> tuple[int, int]:
    """Insert rows, skipping duplicates. Returns (inserted, skipped)."""
    if not rows:
        return 0, 0
    inserted = 0
    for i in range(0, len(rows), 1000):
        batch = [r.db_row() for r in rows[i : i + 1000]]
        stmt = (
            insert(ExternalTrade)
            .values(batch)
            .on_conflict_do_nothing(constraint="uq_external_trades_source_trade")
            .returning(ExternalTrade.id)
        )
        inserted += len((await session.execute(stmt)).all())
    return inserted, len(rows) - inserted


# ---------------------------------------------------------------- API import


async def import_history(client: Any, since_ms: int, now_ms: int, extra_symbols: list[str]) -> dict:
    """Fetch trades read-only. Returns {"rows": [...], "report": {...}} (nothing stored here)."""
    from deskmind.data.mainnet_readonly import FuturesReadNotEnabled

    report: dict[str, Any] = {"futures": {}, "spot": {}, "refused": []}
    rows: list[TradeRow] = []

    # Futures: discover symbols from income history, then fetch fills per symbol.
    try:
        income = await client.get_futures_income(since_ms, now_ms)
        f_symbols = sorted({r["symbol"] for r in income if r.get("symbol")})
        report["futures"]["symbols"] = f_symbols
        for sym in f_symbols:
            fills = await client.get_futures_user_trades(sym, since_ms, now_ms)
            rows.extend(from_futures_api(x) for x in fills)
        report["futures"]["trades"] = sum(1 for r in rows if r.market == "futures")
    except FuturesReadNotEnabled as exc:
        report["futures"] = {"skipped": str(exc)}
        report["refused"].append({"endpoints": ["/fapi/v1/income", "/fapi/v1/userTrades"],
                                  "reason": str(exc)})  # fmt: skip

    # Spot: candidate symbols from current balances (+ configured symbols); invalid ones skipped.
    try:
        balances = await client.get_spot_balances()
        assets = sorted({b["asset"] for b in balances})
        candidates = {f"{a}{q}" for a in assets for q in SPOT_QUOTES if a != q}
        candidates |= set(extra_symbols)
        report["spot"]["assets_with_balance"] = assets
        checked, with_trades = 0, []
        for sym in sorted(candidates):
            try:
                fills = await client.get_spot_my_trades(sym)
            except Exception as exc:  # invalid symbol (-1121) and similar: not an error
                if "-1121" in str(exc) or "400" in str(exc):
                    continue
                raise
            checked += 1
            if fills:
                with_trades.append(sym)
                rows.extend(from_spot_api(x) for x in fills if int(x["time"]) >= since_ms)
        report["spot"].update(
            symbols_checked=checked,
            symbols_with_trades=with_trades,
            trades=sum(1 for r in rows if r.market == "spot"),
            note="symbols come from current balances; sold-out assets need the CSV import",
        )
    except Exception as exc:
        report["spot"] = {"error": str(exc)}
        report["refused"].append({"endpoints": ["/api/v3/account", "/api/v3/myTrades"],
                                  "reason": str(exc)})  # fmt: skip
    return {"rows": rows, "report": report}


# ---------------------------------------------------------------- queries


async def list_trades(
    session: Any,
    *,
    market: str | None = None,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 500,
) -> list[ExternalTrade]:
    q = select(ExternalTrade)
    if market:
        q = q.where(ExternalTrade.market == market)
    if symbol:
        q = q.where(ExternalTrade.symbol == symbol.upper())
    if start:
        q = q.where(ExternalTrade.time >= start)
    if end:
        q = q.where(ExternalTrade.time < end)
    q = q.order_by(ExternalTrade.time.desc()).limit(limit)
    return list((await session.execute(q)).scalars())


def trade_stats(trades: list[ExternalTrade]) -> dict[str, Any]:
    """Per-symbol counts, win/loss where realized PnL exists, fees by asset."""
    per_symbol: Counter[str] = Counter()
    fees: defaultdict[str, Decimal] = defaultdict(Decimal)
    wins = losses = flat = 0
    pnl_total = Decimal(0)
    for t in trades:
        per_symbol[f"{t.market}:{t.symbol}"] += 1
        if t.fee is not None and t.fee_asset:
            fees[t.fee_asset] += t.fee
        if t.realized_pnl is not None and t.realized_pnl != 0:
            pnl_total += t.realized_pnl
            if t.realized_pnl > 0:
                wins += 1
            else:
                losses += 1
        elif t.realized_pnl is not None:
            flat += 1
    closing = wins + losses
    return {
        "trades": len(trades),
        "per_symbol": dict(per_symbol.most_common()),
        "fees_by_asset": {k: str(v) for k, v in sorted(fees.items())},
        "pnl_fills": {
            "winning": wins,
            "losing": losses,
            "zero_pnl": flat,
            "win_rate": (wins / closing) if closing else None,
            "realized_pnl_total": str(pnl_total),
            "note": "counted per fill with non-zero realized PnL (futures only)",
        },
    }


async def count_by_market(session: Any) -> dict[str, int]:
    rows = (
        await session.execute(
            select(ExternalTrade.market, func.count()).group_by(ExternalTrade.market)
        )
    ).all()
    return {m: n for m, n in rows}
