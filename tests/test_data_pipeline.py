"""End-to-end sync against fake data.binance.vision + fapi servers (no network)."""

from datetime import UTC, date, datetime

import httpx

from deskmind.data import bulk_download as bulk
from deskmind.data.datasets import FUNDING, HOUR_MS, Dataset, to_ms
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.pipeline import Syncer, missing_runs
from deskmind.data.storage import Store, read_parquet, schema_for, time_values_ms
from deskmind.data.verify import verify_store
from tests.data_fixtures import FakeClock, checksum_file, kline_row, make_zip

H1 = Dataset("klines", "1h")
JAN_START = to_ms(datetime(2024, 1, 1, tzinfo=UTC))
FEB_START = to_ms(datetime(2024, 2, 1, tzinfo=UTC))
# Genuine exchange gap: 2024-01-30 10:00 and 11:00 exist nowhere (bulk nor REST).
GAP = {to_ms(datetime(2024, 1, 30, 10, tzinfo=UTC)), to_ms(datetime(2024, 1, 30, 11, tzinfo=UTC))}
FUNDING_HDR = "calc_time,funding_interval_hours,last_funding_rate"


BASE = {"BTCUSDT": 20000.0, "ETHUSDT": 1500.0, "SOLUSDT": 90.0}


def candles(lo: int, hi: int, symbol: str = "BTCUSDT") -> list[list[str]]:
    return [kline_row(t, HOUR_MS, BASE[symbol]) for t in range(lo, hi, HOUR_MS) if t not in GAP]


class FakeBinance:
    """Fake data.binance.vision + fapi for one or more symbols (distinct price levels)."""

    def __init__(self, symbols: tuple[str, ...] = ("BTCUSDT",)) -> None:
        self.server_now = 0
        self.vision_paths: list[str] = []
        self.fapi_paths: list[str] = []
        self.fapi_symbols: list[str] = []
        self.files: dict[str, bytes] = {}
        for sym in symbols:
            jan = make_zip(f"{sym}-1h-2024-01.csv", candles(JAN_START, FEB_START, sym))
            feb1 = make_zip(
                f"{sym}-1h-2024-02-01.csv", candles(FEB_START, FEB_START + 24 * HOUR_MS, sym)
            )
            fund = make_zip(
                "f.csv",
                [[str(t + 7), "8", "0.0001"] for t in range(JAN_START, FEB_START, 8 * HOUR_MS)],
                header=FUNDING_HDR,
            )
            for path, data in (
                (bulk.monthly_url(sym, H1, 2024, 1), jan),
                (bulk.daily_url(sym, H1, date(2024, 2, 1)), feb1),
                (bulk.monthly_url(sym, FUNDING, 2024, 1), fund),
            ):
                assert path is not None
                self.files[path] = data
                self.files[path + ".CHECKSUM"] = checksum_file(data, path.rsplit("/", 1)[1])

    def vision_handler(self, request: httpx.Request) -> httpx.Response:
        self.vision_paths.append(request.url.path)
        body = self.files.get(request.url.path)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    def fapi_handler(self, request: httpx.Request) -> httpx.Response:
        self.fapi_paths.append(request.url.path)
        q = request.url.params
        self.fapi_symbols.append(q.get("symbol", ""))
        start, end, limit = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
        if request.url.path == "/fapi/v1/klines":
            # Like Binance: includes the still-forming candle.
            opens = [
                t
                for t in range(start - start % HOUR_MS, end + 1, HOUR_MS)
                if start <= t <= end and t <= self.server_now and t not in GAP
            ]
            base = BASE[q["symbol"]]
            return httpx.Response(200, json=[kline_row(t, HOUR_MS, base) for t in opens[:limit]])
        if request.url.path == "/fapi/v1/fundingRate":
            ev = [
                {"fundingTime": t + 2, "fundingRate": "0.0001", "markPrice": "20000.0"}
                for t in range(JAN_START, end + 1, 8 * HOUR_MS)
                if start <= t + 2 <= end and t + 2 <= self.server_now
            ]
            return httpx.Response(200, json=ev[:limit])
        return httpx.Response(404)

    def clients(self) -> tuple[RateLimitedClient, RateLimitedClient]:
        clock = FakeClock()
        fapi = RateLimitedClient(
            "https://fapi.test",
            transport=httpx.MockTransport(self.fapi_handler),
            sleep=clock.sleep,
            clock=clock,
        )
        vision = RateLimitedClient(
            "https://vision.test",
            weight_limit=None,
            transport=httpx.MockTransport(self.vision_handler),
            sleep=clock.sleep,
            clock=clock,
        )
        return fapi, vision


async def sync(fake: FakeBinance, store: Store, now: datetime, start: date | None):
    fake.server_now = to_ms(now)
    fake.vision_paths.clear()
    fake.fapi_paths.clear()
    fake.fapi_symbols.clear()
    fapi, vision = fake.clients()
    async with fapi, vision:
        return await Syncer(store, fapi, vision, now=now).run(start, datasets=(H1, FUNDING))


def rows(store: Store, ds: Dataset, y: int, m: int) -> list[int]:
    t = read_parquet(store.partition_path(ds, y, m), schema_for(ds))
    return [] if t is None else time_values_ms(t, "open_time" if ds.is_candles else "funding_time")


def test_missing_runs() -> None:
    assert missing_runs([0, 1, 2, 5, 7, 8], 1) == [(0, 3), (5, 6), (7, 9)]
    assert missing_runs([], 1) == []


async def test_sync_is_incremental_and_idempotent(tmp_path) -> None:
    fake = FakeBinance()
    store = Store(tmp_path, "BTCUSDT")
    now = datetime(2024, 2, 2, 5, 30, tzinfo=UTC)  # 05:00 candle still forming

    # Run 1: January from the monthly zip, Feb 1 from the daily zip, Feb 2 via REST.
    res = {r.key: r for r in await sync(fake, store, now, date(2024, 1, 30))}
    jan, feb = rows(store, H1, 2024, 1), rows(store, H1, 2024, 2)
    assert len(jan) == 31 * 24 - len(GAP)
    assert len(feb) == 24 + 5  # Feb 1 + Feb 2 00:00..04:00; forming 05:00 excluded
    assert max(feb) == to_ms(datetime(2024, 2, 2, 4, tzinfo=UTC))
    assert res["klines/1h"].monthly_zips == 1 and res["klines/1h"].daily_zips == 1
    assert res["klines/1h"].missing_candles == len(GAP)
    manifest = store.load_manifest()["datasets"]["klines/1h"]
    assert manifest["2024-01"]["complete"] and manifest["2024-01"]["missing"] == 2
    assert not manifest["2024-02"]["complete"]
    assert len(rows(store, FUNDING, 2024, 1)) == 93 + 4  # Jan events + Feb 1-2 via REST
    snapshot = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.parquet")}

    # Run 2, same moment: nothing new -> no bulk requests, no kline requests, identical files.
    await sync(fake, store, now, None)
    assert fake.vision_paths == []
    assert "/fapi/v1/klines" not in fake.fapi_paths
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.parquet")
    } == snapshot

    # Run 3, two hours later: only the two newly closed candles are fetched.
    later = datetime(2024, 2, 2, 7, 30, tzinfo=UTC)
    res3 = {r.key: r for r in await sync(fake, store, later, None)}
    assert res3["klines/1h"].rows_added == 2
    assert res3["klines/1h"].months_skipped == 1  # January untouched
    assert fake.vision_paths == []  # today's daily zip is never requested
    assert len(set(rows(store, H1, 2024, 2))) == len(rows(store, H1, 2024, 2)) == 31

    # Verify sees the genuine gap as a source gap and nothing else wrong.
    rep = verify_store(store, datasets=(H1, FUNDING), now=later)
    h1 = next(d for d in rep.datasets if d.key == "klines/1h")
    assert [(g.missing, "source gap" in g.note) for g in h1.gaps] == [(2, True)]
    assert all(c.status == "PASS" for c in h1.checks if c.name != "no gaps")


async def test_multiple_symbols_are_kept_separate(tmp_path) -> None:
    symbols = ("BTCUSDT", "ETHUSDT")
    fake = FakeBinance(symbols)
    now = datetime(2024, 2, 2, 5, 30, tzinfo=UTC)
    for sym in symbols:
        await sync(fake, Store(tmp_path, sym), now, date(2024, 1, 30))
        assert all(f"/{sym}/" in p or f"{sym}-" in p for p in fake.vision_paths)

    btc, eth = Store(tmp_path, "BTCUSDT"), Store(tmp_path, "ETHUSDT")
    assert btc.root != eth.root and btc.manifest_path.exists() and eth.manifest_path.exists()
    t_btc = read_parquet(btc.partition_path(H1, 2024, 2), schema_for(H1))
    t_eth = read_parquet(eth.partition_path(H1, 2024, 2), schema_for(H1))
    assert t_btc is not None and t_eth is not None
    assert t_btc.num_rows == t_eth.num_rows == 29
    # Price levels prove no cross-contamination between symbols.
    assert min(t_btc.column("low").to_pylist()) > 10_000
    assert max(t_eth.column("high").to_pylist()) < 10_000

    # Re-running one symbol does not touch the other.
    eth_before = eth.partition_path(H1, 2024, 2).read_bytes()
    await sync(fake, btc, datetime(2024, 2, 2, 7, 30, tzinfo=UTC), None)
    assert eth.partition_path(H1, 2024, 2).read_bytes() == eth_before
    assert fake.fapi_symbols and set(fake.fapi_symbols) == {"BTCUSDT"}
    assert all("ETHUSDT" not in p for p in fake.vision_paths)
