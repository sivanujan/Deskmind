"""Gap fill and recent data from Binance USDT-M public REST endpoints (no API key)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from deskmind.data.datasets import FUNDING, Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.storage import candles_table, funding_table, merge_tables

KLINE_LIMIT = 1500
FUNDING_LIMIT = 1000


def kline_weight(limit: int) -> int:
    """Request weight of /fapi/v1/klines and /fapi/v1/markPriceKlines for a given limit."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


async def fetch_candles_range(
    client: RateLimitedClient, symbol: str, ds: Dataset, start_ms: int, end_ms: int
) -> pa.Table:
    """All candles with start_ms <= open_time < end_ms (paginated)."""
    interval = ds.interval_ms
    rows: list[list[Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        remaining = (end_ms - cursor + interval - 1) // interval
        limit = int(min(KLINE_LIMIT, max(remaining, 1)))
        batch = await client.get_json(
            ds.rest_path,
            {
                "symbol": symbol,
                "interval": ds.timeframe,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": limit,
            },
            weight=kline_weight(limit),
        )
        if not batch:
            break
        rows.extend(r for r in batch if start_ms <= int(r[0]) < end_ms)
        next_cursor = int(batch[-1][0]) + interval
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    return merge_tables(ds, candles_table(ds, rows))


async def fetch_funding_range(
    client: RateLimitedClient, symbol: str, start_ms: int, end_ms: int
) -> pa.Table:
    """Funding events with start_ms <= fundingTime < end_ms (paginated)."""
    events: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = await client.get_json(
            FUNDING.rest_path,
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": FUNDING_LIMIT},
            weight=1,
        )
        if not batch:
            break
        events.extend(e for e in batch if start_ms <= int(e["fundingTime"]) < end_ms)
        if len(batch) < FUNDING_LIMIT:
            break
        cursor = int(batch[-1]["fundingTime"]) + 1
    table = funding_table(
        [int(e["fundingTime"]) for e in events],
        [float(e["fundingRate"]) for e in events],
        mark_prices=[float(e["markPrice"]) if e.get("markPrice") else None for e in events],
    )
    return merge_tables(FUNDING, table)


async def fetch_symbol_filters(client: RateLimitedClient, symbol: str) -> dict[str, Any]:
    """Exchange filters snapshot for one symbol. Values kept as strings (Decimal-safe)."""
    info = await client.get_json("/fapi/v1/exchangeInfo", weight=1)
    sym = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
    if sym is None:
        raise ValueError(f"{symbol} not found in exchangeInfo")
    filters = {f["filterType"]: f for f in sym["filters"]}
    return {
        "symbol": symbol,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "/fapi/v1/exchangeInfo",
        "summary": {
            "tickSize": filters["PRICE_FILTER"]["tickSize"],
            "stepSize": filters["LOT_SIZE"]["stepSize"],
            "minQty": filters["LOT_SIZE"]["minQty"],
            "maxQty": filters["LOT_SIZE"]["maxQty"],
            "marketStepSize": filters.get("MARKET_LOT_SIZE", {}).get("stepSize"),
            "marketMaxQty": filters.get("MARKET_LOT_SIZE", {}).get("maxQty"),
            "minNotional": filters.get("MIN_NOTIONAL", {}).get("notional"),
            "pricePrecision": sym.get("pricePrecision"),
            "quantityPrecision": sym.get("quantityPrecision"),
        },
        "contract": {
            k: sym.get(k)
            for k in (
                "contractType",
                "status",
                "marginAsset",
                "quoteAsset",
                "onboardDate",
                "liquidationFee",
                "marketTakeBound",
                "triggerProtect",
            )
        },
        "filters": sym["filters"],
        "rate_limits": info.get("rateLimits"),
    }
