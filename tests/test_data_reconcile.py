"""Reconcile: bulk placeholders replaced by REST; exchange-side mismatches confirmed (offline)."""

import httpx

from deskmind.data.datasets import MINUTE_MS, Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.reconcile import reconcile
from deskmind.data.storage import (
    Store,
    candles_table,
    create_view_from_arrow,
    duckdb_connect,
    read_parquet,
    schema_for,
    write_parquet,
)
from deskmind.data.verify import FAIL, PASS, WARN, check_cross_tf, verify_store
from tests.data_fixtures import FakeClock, kline_row, resample_rows

M5, M15 = Dataset("klines", "5m"), Dataset("klines", "15m")
IV = 5 * MINUTE_MS
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
N = 36  # 3 hours of 5m


def placeholder(row: list[str], prev_close: str) -> list[str]:
    """What the bulk archive contains during some incidents: flat, zero volume, zero trades."""
    return [
        row[0],
        prev_close,
        prev_close,
        prev_close,
        prev_close,
        "0",
        row[6],
        "0",
        "0",
        "0",
        "0",
        "0",
    ]


def test_resample_ignores_zero_trade_candles() -> None:
    """A genuine no-trade 5m candle must not set the 15m open (Binance's own rule)."""
    rows5 = [kline_row(T0 + i * IV, IV) for i in range(6)]
    rows15 = resample_rows(rows5, 3, IV)
    rows5[0] = placeholder(rows5[0], "19000.00")  # first candle of bucket had no trades
    con = duckdb_connect()
    create_view_from_arrow(con, "s", candles_table(M5, rows5))
    # Build the expected 15m from traded candles only: bucket 0 = candles 1..2.
    traded = rows5[1:3]
    exp = [
        str(T0),
        traded[0][1],
        f"{max(float(r[2]) for r in traded):.2f}",
        f"{min(float(r[3]) for r in traded):.2f}",
        traded[-1][4],
        f"{sum(float(r[5]) for r in traded):.3f}",
        str(T0 + 3 * IV - 1),
        "0",
        "1",
        "0",
        "0",
        "0",
    ]
    create_view_from_arrow(con, "d", candles_table(M15, [exp, rows15[1]]))
    rep = check_cross_tf(con, "s", "d", M5, M15)
    assert rep.mismatches == 0, rep.examples
    assert rep.status == PASS


def test_confirmed_mismatch_is_warn_not_fail() -> None:
    rows5 = [kline_row(T0 + i * IV, IV) for i in range(6)]
    rows15 = resample_rows(rows5, 3, IV)
    rows15[0][2] = str(float(rows15[0][2]) + 5)
    con = duckdb_connect()
    create_view_from_arrow(con, "s", candles_table(M5, rows5))
    create_view_from_arrow(con, "d", candles_table(M15, rows15))
    assert check_cross_tf(con, "s", "d", M5, M15).status == FAIL
    rep = check_cross_tf(con, "s", "d", M5, M15, confirmed={T0})
    assert rep.mismatches == 0 and rep.confirmed_source == 1 and rep.status == WARN


class FakeRest:
    """REST knows the real candles; 15m bucket at T0+1h is truncated on the exchange itself."""

    def __init__(self) -> None:
        self.real5 = [kline_row(T0 + i * IV, IV) for i in range(N)]
        self.real15 = resample_rows(self.real5, 3, IV)
        self.real15[4] = [*self.real15[4]]
        self.real15[4][2] = self.real5[12][2]  # exchange 15m high = first 5m only (incident)
        self.real15[4][3] = self.real5[12][3]
        self.real15[4][4] = self.real5[12][4]
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        q = request.url.params
        src = self.real5 if q["interval"] == "5m" else self.real15
        lo, hi = int(q["startTime"]), int(q["endTime"])
        return httpx.Response(200, json=[r for r in src if lo <= int(r[0]) <= hi])

    def client(self) -> RateLimitedClient:
        clock = FakeClock()
        return RateLimitedClient(
            "https://fapi.test",
            transport=httpx.MockTransport(self.handler),
            sleep=clock.sleep,
            clock=clock,
        )


async def test_reconcile_replaces_placeholders_and_confirms_exchange_side(tmp_path) -> None:
    fake = FakeRest()
    store = Store(tmp_path, "BTCUSDT")
    bulk5 = [list(r) for r in fake.real5]
    for i in (20, 21):  # bulk archive placeholders where REST has real trades
        bulk5[i] = placeholder(bulk5[i], bulk5[19][4])
    bulk15 = [list(r) for r in fake.real15]
    write_parquet(store.partition_path(M5, 2024, 1), candles_table(M5, bulk5))
    write_parquet(store.partition_path(M15, 2024, 1), candles_table(M15, bulk15))
    store.save_manifest({"start": "2024-01-01", "datasets": {}})

    async with fake.client() as fapi:
        res = await reconcile(store, fapi, datasets=(M5, M15))
    assert res.zero_trade_checked["klines/5m"] == 2
    assert res.replaced["klines/5m"] == 2
    assert res.cross_tf_confirmed["klines/5m->klines/15m"] == 1

    stored = read_parquet(store.partition_path(M5, 2024, 1), schema_for(M5))
    assert stored is not None and stored.equals(candles_table(M5, fake.real5))

    rec = store.load_manifest()["reconcile"]
    assert rec["cross_tf_confirmed"]["klines/5m->klines/15m"] == [T0 + 3600_000]
    assert sorted(rec["klines/5m"]["replaced"]) == [T0 + 20 * IV, T0 + 21 * IV]

    # Second run: nothing left to check -> no requests at all.
    fake.calls.clear()
    async with fake.client() as fapi:
        await reconcile(store, fapi, datasets=(M5, M15))
    assert fake.calls == []

    rep = verify_store(store, datasets=(M5, M15), now=None)
    xtf = rep.cross_tf[0]
    assert xtf.mismatches == 0 and xtf.confirmed_source == 1
    assert next(d for d in rep.datasets if d.key == "klines/5m").status in (PASS, WARN)
