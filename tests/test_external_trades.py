"""Import of the user's own Binance history into external_trades.

Network is always mocked. The dedupe test uses the Deskmind Postgres (port 5435) inside a
transaction that is rolled back, and is skipped when the database is not reachable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from deskmind.config import Settings
from deskmind.data.external_trades import (
    CsvFormatError,
    TradeRow,
    import_history,
    parse_trade_csv,
    trade_stats,
    upsert_trades,
)
from deskmind.data.mainnet_readonly import (
    ALLOWLISTED_ENDPOINTS,
    FUTURES_NOT_ENABLED,
    FuturesReadNotEnabled,
    MainnetReadOnlyClient,
    MainnetReadOnlySecurityViolation,
)

T0 = 1_750_000_000_000  # ms


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, handler) -> None:
        self.handler = handler
        self.log: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        return self.handler(request)


def make_client(handler) -> tuple[MainnetReadOnlyClient, Transport]:
    settings = Settings(
        trading_mode="testnet",
        binance_mainnet_readonly_api_key=SecretStr("mock_key"),
        binance_mainnet_readonly_api_secret=SecretStr("mock_secret"),
        data_symbols=["BTCUSDT"],
    )
    t = Transport(handler)
    return MainnetReadOnlyClient(settings=settings, http_client=httpx.AsyncClient(transport=t)), t


def q(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}


FUT_FILL = {
    "symbol": "BTCUSDT",
    "id": 101,
    "orderId": 9001,
    "side": "SELL",
    "price": "65000.1",
    "qty": "0.010",
    "quoteQty": "650.001",
    "realizedPnl": "12.5",
    "commission": "0.26",
    "commissionAsset": "USDT",
    "time": T0 + 1000,
}
SPOT_FILL = {
    "symbol": "ETHUSDT",
    "id": 55,
    "orderId": 77,
    "price": "3000",
    "qty": "0.5",
    "quoteQty": "1500",
    "commission": "0.0005",
    "commissionAsset": "ETH",
    "time": T0 + 2000,
    "isBuyer": True,
}


# ---------------------------------------------------------------- allowlist / double lock


def test_history_endpoints_are_allowlisted_get_only():
    for path in ("/fapi/v1/userTrades", "/fapi/v1/income", "/api/v3/myTrades", "/api/v3/account"):
        assert path in ALLOWLISTED_ENDPOINTS
    for path in ("/fapi/v1/order", "/api/v3/order", "/sapi/v1/asset/transfer"):
        assert path not in ALLOWLISTED_ENDPOINTS


@pytest.mark.asyncio
async def test_history_calls_still_refuse_non_get():
    client, t = make_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(MainnetReadOnlySecurityViolation):
        await client.request("POST", "/api/v3/myTrades")
    with pytest.raises(MainnetReadOnlySecurityViolation):
        await client.request("GET", "/api/v3/order")
    assert t.log == []


# ---------------------------------------------------------------- mocked endpoints


@pytest.mark.asyncio
async def test_futures_user_trades_walks_7_day_windows_and_signs():
    client, t = make_client(lambda r: httpx.Response(200, json=[FUT_FILL]))
    start, end = T0, T0 + 20 * 86_400_000
    rows = await client.get_futures_user_trades("BTCUSDT", start, end)
    assert len(t.log) == 3  # 20 days -> three windows of at most 7 days
    for req in t.log:
        params = q(req)
        assert req.method == "GET"
        assert req.url.host == "fapi.binance.com"
        assert req.url.path == "/fapi/v1/userTrades"
        assert int(params["endTime"]) - int(params["startTime"]) < 7 * 86_400_000
        assert "signature" in params
        assert req.headers["X-MBX-APIKEY"] == "mock_key"
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_spot_my_trades_pages_by_from_id():
    page1 = [dict(SPOT_FILL, id=i) for i in range(1000)]
    page2 = [dict(SPOT_FILL, id=1000)]

    def handler(req):
        return httpx.Response(200, json=page1 if q(req)["fromId"] == "0" else page2)

    client, t = make_client(handler)
    rows = await client.get_spot_my_trades("ETHUSDT")
    assert len(rows) == 1001
    assert [q(r)["fromId"] for r in t.log] == ["0", "1000"]
    assert all(r.url.host == "api.binance.com" for r in t.log)


@pytest.mark.asyncio
async def test_import_history_maps_futures_and_spot():
    def handler(req):
        path = req.url.path
        if path == "/fapi/v1/income":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "time": T0}])
        if path == "/fapi/v1/userTrades":
            return httpx.Response(200, json=[FUT_FILL])
        if path == "/api/v3/account":
            return httpx.Response(200, json={"balances": [{"asset": "ETH"}]})
        if path == "/api/v3/myTrades":
            if q(req)["symbol"] == "ETHUSDT":
                return httpx.Response(200, json=[SPOT_FILL])
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        return httpx.Response(404)

    client, _ = make_client(handler)
    res = await import_history(client, T0, T0 + 86_400_000, ["BTCUSDT"])
    rows = res["rows"]
    fut = [r for r in rows if r.market == "futures"]
    spot = [r for r in rows if r.market == "spot"]
    assert len(fut) == 1
    assert fut[0].realized_pnl == Decimal("12.5")
    assert fut[0].side == "SELL"
    assert fut[0].trade_id == "101"
    assert len(spot) == 1
    assert spot[0].side == "BUY"
    assert spot[0].realized_pnl is None
    assert spot[0].fee_asset == "ETH"
    assert res["report"]["refused"] == []
    assert res["report"]["spot"]["symbols_with_trades"] == ["ETHUSDT"]


# ---------------------------------------------------------------- permission denied


@pytest.mark.asyncio
async def test_futures_permission_denied_is_skipped_gracefully():
    def handler(req):
        if req.url.path.startswith("/fapi/"):
            return httpx.Response(
                401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
            )
        if req.url.path == "/api/v3/account":
            return httpx.Response(200, json={"balances": []})
        return httpx.Response(200, json=[])

    client, t = make_client(handler)
    res = await import_history(client, T0, T0 + 86_400_000, [])
    assert res["report"]["futures"] == {"skipped": FUTURES_NOT_ENABLED}
    assert res["report"]["refused"][0]["reason"] == FUTURES_NOT_ENABLED
    assert "please enable" not in FUTURES_NOT_ENABLED.lower()  # never suggests enabling it
    # after one refusal no further futures request is sent
    fut_calls = [r for r in t.log if r.url.path.startswith("/fapi/")]
    assert len(fut_calls) == 1
    with pytest.raises(FuturesReadNotEnabled):
        await client.get_futures_income(T0, T0 + 1)
    assert len([r for r in t.log if r.url.path.startswith("/fapi/")]) == 1


@pytest.mark.asyncio
async def test_futures_disabled_by_permissions_never_calls_network():
    client, t = make_client(lambda r: httpx.Response(200, json=[]))
    client.futures_enabled = False
    with pytest.raises(FuturesReadNotEnabled, match="not enabled \\(by design\\)"):
        await client.get_futures_user_trades("BTCUSDT", T0, T0 + 1)
    assert t.log == []


# ---------------------------------------------------------------- CSV parsing

SPOT_OLD = """Date(UTC),Pair,Side,Price,Executed,Amount,Fee
2024-03-01 10:00:00,BTCUSDT,BUY,62000.5,0.00100000BTC,62.0005USDT,0.00000100BTC
2024-03-02 11:30:00,BTCUSDT,SELL,63000,0.00100000BTC,63USDT,0.063USDT
"""

SPOT_NEW = """Date(UTC),Market,Type,Price,Amount,Total,Fee,Fee Coin
2025-01-05 08:00:00,ETHUSDT,BUY,3300.1,0.5,1650.05,0.0005,ETH
"""

FUTURES = """Date(UTC),Symbol,Side,Price,Quantity,Amount,Fee,Realized Profit
2025-02-01 12:00:00,BTCUSDT,BUY,100000,0.002,200,0.08 USDT,0
2025-02-01 15:00:00,BTCUSDT,SELL,101000,0.002,202,0.0808 USDT,2
2025-02-02 09:00:00,SOLUSDT,SELL,200,1,200,0.08 USDT,-1.5
"""


def test_parse_spot_old_layout_with_assets_in_values():
    market, rows = parse_trade_csv(SPOT_OLD)
    assert market == "spot"
    assert len(rows) == 2
    b = rows[0]
    assert (b.symbol, b.side) == ("BTCUSDT", "BUY")
    assert (b.price, b.qty) == (Decimal("62000.5"), Decimal("0.001"))
    assert b.quote_qty == Decimal("62.0005")
    assert (b.fee, b.fee_asset) == (Decimal("0.000001"), "BTC")
    assert b.time == datetime(2024, 3, 1, 10, 0, tzinfo=UTC)
    assert b.realized_pnl is None and b.source == "csv"
    assert rows[1].fee_asset == "USDT"


def test_parse_spot_new_layout():
    market, rows = parse_trade_csv(SPOT_NEW)
    assert market == "spot"
    r = rows[0]
    assert (r.symbol, r.qty, r.quote_qty, r.fee, r.fee_asset) == (
        "ETHUSDT",
        Decimal("0.5"),
        Decimal("1650.05"),
        Decimal("0.0005"),
        "ETH",
    )


def test_parse_futures_layout_detects_market_and_pnl():
    market, rows = parse_trade_csv(FUTURES)
    assert market == "futures"
    assert [r.realized_pnl for r in rows] == [Decimal("0"), Decimal("2"), Decimal("-1.5")]
    assert rows[0].qty == Decimal("0.002")
    assert rows[0].quote_qty == Decimal("200")
    assert (rows[0].fee, rows[0].fee_asset) == (Decimal("0.08"), "USDT")
    stats = trade_stats(rows)  # type: ignore[arg-type]
    assert stats["pnl_fills"]["winning"] == 1
    assert stats["pnl_fills"]["losing"] == 1
    assert stats["pnl_fills"]["zero_pnl"] == 1
    assert stats["fees_by_asset"] == {"USDT": "0.2408"}


def test_csv_rows_without_trade_id_get_stable_ids():
    _, a = parse_trade_csv(FUTURES)
    _, b = parse_trade_csv(FUTURES)
    assert [r.trade_id for r in a] == [r.trade_id for r in b]
    assert len({r.trade_id for r in a}) == 3
    assert all(r.trade_id.startswith("csv-") for r in a)


def test_unknown_csv_layout_is_rejected_with_headers():
    with pytest.raises(CsvFormatError, match="headers found"):
        parse_trade_csv("foo,bar\n1,2\n")


# ---------------------------------------------------------------- dedupe (Postgres, rolled back)


def _row(trade_id: str, source: str = "csv", market: str = "futures") -> TradeRow:
    return TradeRow(
        source=source,
        market=market,
        symbol="ZZTESTUSDT",
        side="BUY",
        price=Decimal("1"),
        qty=Decimal("1"),
        quote_qty=None,
        fee=None,
        fee_asset=None,
        realized_pnl=None,
        time=datetime(2020, 1, 1, tzinfo=UTC),
        order_id=None,
        trade_id=trade_id,
    )


def test_upsert_dedupes_on_source_market_trade_id():
    from sqlalchemy.ext.asyncio import AsyncSession

    from deskmind.db.session import create_engine

    async def go() -> None:
        engine = create_engine()
        try:
            async with engine.connect() as conn:
                trans = await conn.begin()
                try:
                    session = AsyncSession(bind=conn)
                    first = await upsert_trades(session, [_row("1"), _row("2")])
                    again = await upsert_trades(session, [_row("1"), _row("2"), _row("3")])
                    other = await upsert_trades(
                        session, [_row("1", source="api"), _row("1", market="spot")]
                    )
                    assert first == (2, 0)
                    assert again == (1, 2)
                    assert other == (2, 0)  # same trade id, different source / market
                finally:
                    await trans.rollback()
        finally:
            await engine.dispose()

    try:
        asyncio.run(go())
    except (OSError, ConnectionError) as exc:  # pragma: no cover - DB not running
        pytest.skip(f"Deskmind Postgres not reachable: {exc}")
