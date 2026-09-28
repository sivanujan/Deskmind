"""Liquidity universe + daily job. Everything mocked: no network, no Postgres, no disk writes."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, date, datetime

import pytest

from deskmind.universe.daily import DailyJob, can_open_new_entry, seconds_until_next_run
from deskmind.universe.models import SymbolStat
from deskmind.universe.ranking import DAY_MS, UniverseConfig, fetch_liquidity_stats, rank_universe
from deskmind.universe.store import MemoryUniverseStore

NOW = int(datetime(2026, 9, 28, 0, 1, tzinfo=UTC).timestamp() * 1000)
OLD = NOW - 1000 * DAY_MS  # listed long ago
CFG = UniverseConfig(top_n=3, exit_rank=4, min_listing_age_days=180, download_concurrency=2)


def stat(sym: str, vol: float, onboard: int = OLD) -> SymbolStat:
    return SymbolStat(sym, onboard, vol, 30)


class FakeOps:
    def __init__(self, stats, have_data=(), fail_download=(), fail_verify=(), used_pct=50.0):
        self.stats = list(stats)
        self.have = set(have_data)
        self.fail_download = set(fail_download)
        self.fail_verify = set(fail_verify)
        self.used_pct = used_pct
        self.calls: list[tuple] = []
        self.deleted: list[str] = []  # the job must never delete data
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_stats(self, include, now_ms):
        self.calls.append(("fetch_stats", tuple(sorted(include))))
        return list(self.stats)

    def has_data(self, symbol):
        return symbol in self.have

    async def _io(self, name, symbol, *args):
        self.calls.append((name, symbol, *args))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1

    async def download(self, symbol, start):
        await self._io("download", symbol, start)
        if symbol in self.fail_download:
            raise RuntimeError("HTTP 503 from data.binance.vision")
        self.have.add(symbol)

    async def update(self, symbol):
        await self._io("update", symbol)

    async def verify(self, symbol):
        self.calls.append(("verify", symbol))
        if symbol in self.fail_verify:
            return False, "klines_5m: gaps: 12 missing candles"
        return True, "PASS"

    async def warm_up(self, symbol, start_ms, end_ms):
        self.calls.append(("warm_up", symbol, start_ms, end_ms))

    def disk_usage(self):
        return {"used_pct": self.used_pct, "free_bytes": 10**11, "total_bytes": 10**12,
                "data_dir_bytes": 5 * 10**9}  # fmt: skip

    def names(self, name):
        return [c[1] for c in self.calls if c[0] == name]


async def run(store, ops, now=NOW, cfg=CFG):
    return await DailyJob(store, ops, cfg, now_ms=lambda: now).run()


# ---------------------------------------------------------------- ranking (liquidity only)


def test_rank_is_liquidity_only_with_exit_buffer():
    stats = [stat("AAA", 900), stat("BBB", 800), stat("CCC", 700), stat("DDD", 600),
             stat("EEE", 500)]  # fmt: skip
    r = rank_universe(stats, set(), CFG)
    assert r.members == ["AAA", "BBB", "CCC"]
    assert r.entered == ["AAA", "BBB", "CCC"]
    # DDD (rank 4) is inside the exit buffer: an existing member stays, a newcomer does not enter
    r2 = rank_universe(stats, {"AAA", "BBB", "DDD"}, CFG)
    assert r2.members == ["AAA", "BBB", "CCC", "DDD"]
    assert r2.entered == ["CCC"] and r2.exited == []
    # EEE (rank 5) is beyond exit_rank: it leaves
    r3 = rank_universe(stats, {"EEE"}, CFG)
    assert r3.exited == ["EEE"]
    # SymbolStat has no volatility / trend field: the universe cannot rank on them
    assert {f.name for f in dataclasses.fields(SymbolStat)} == {
        "symbol", "onboard_ms", "median_quote_volume", "days"
    }  # fmt: skip


@pytest.mark.asyncio
async def test_fetch_uses_closed_daily_candles_and_median_quote_volume():
    today = NOW - NOW % DAY_MS

    class Client:
        def __init__(self):
            self.paths = []

        async def get_json(self, path, params=None, weight=1):
            self.paths.append((path, params))
            if path == "/fapi/v1/exchangeInfo":
                return {"symbols": [
                    {"symbol": "AAAUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                     "status": "TRADING", "onboardDate": OLD},
                    {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                     "status": "SETTLING", "onboardDate": OLD},
                    {"symbol": "AAAUSDC", "contractType": "PERPETUAL", "quoteAsset": "USDC",
                     "status": "TRADING", "onboardDate": OLD},
                ]}  # fmt: skip
            if path == "/fapi/v1/ticker/24hr":
                return [{"symbol": "AAAUSDT", "quoteVolume": "5"}, {"symbol": "OLDUSDT"}]
            # 3 closed days + the forming day (huge volume that must be ignored)
            days = [today - 3 * DAY_MS, today - 2 * DAY_MS, today - DAY_MS, today]
            vols = ["100", "300", "200", "999999"]
            return [[d, 0, 0, 0, 0, 0, d + DAY_MS - 1, v] for d, v in zip(days, vols, strict=True)]

    c = Client()
    stats = await fetch_liquidity_stats(c, CFG, NOW, include=set())
    assert stats == [SymbolStat("AAAUSDT", OLD, 200.0, 3)]
    assert all(p[0].startswith("/fapi/v1/") for p in c.paths)


# ---------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_entry_triggers_download_verify_warmup_then_tradeable():
    store, ops = MemoryUniverseStore(), FakeOps([stat("AAA", 900)])
    summary = await run(store, ops)
    assert ops.names("download") == ["AAA"]
    start = next(c[2] for c in ops.calls if c[0] == "download")
    assert start == date(2026, 9, 28) - __import__("datetime").timedelta(days=365 + 30)
    assert ops.names("verify") == ["AAA"] and ops.names("warm_up") == ["AAA"]
    rec = store.records["AAA"]
    assert rec.state == "tradeable" and rec.data_verified and rec.warmed_up
    assert [h["to"] for h in rec.history] == ["downloading", "verifying", "warming_up", "tradeable"]
    assert summary["entered"] == ["AAA"] and summary["downloads_done"] == ["AAA"]


@pytest.mark.asyncio
async def test_entered_symbol_with_data_is_updated_not_redownloaded():
    store, ops = MemoryUniverseStore(), FakeOps([stat("AAA", 900)], have_data={"AAA"})
    await run(store, ops)
    assert ops.names("download") == [] and ops.names("update") == ["AAA"]
    assert store.records["AAA"].state == "tradeable"


@pytest.mark.asyncio
async def test_existing_member_only_updates():
    store, ops = MemoryUniverseStore(), FakeOps([stat("AAA", 900)])
    await run(store, ops)
    ops.calls.clear()
    summary = await run(store, ops, now=NOW + DAY_MS)
    assert ops.names("update") == ["AAA"]
    assert ops.names("download") == [] and ops.names("verify") == [] and ops.names("warm_up") == []
    assert summary["entered"] == [] and summary["updated"] == ["AAA"]
    assert store.records["AAA"].state == "tradeable"


@pytest.mark.asyncio
async def test_verification_failure_blocks_trading_and_alerts():
    store = MemoryUniverseStore()
    ops = FakeOps([stat("AAA", 900)], fail_verify={"AAA"})
    summary = await run(store, ops)
    rec = store.records["AAA"]
    assert rec.state == "blocked" and not rec.data_verified
    assert "verification failed" in rec.reason
    assert ops.names("warm_up") == []
    assert any("verification failed" in a for a in summary["alerts"])
    assert summary["verify_failed"][0]["symbol"] == "AAA"
    ok, why = await can_open_new_entry("AAA", store, ["AAA"])
    assert not ok and "blocked" in why
    # next day it is retried and, once verification passes, becomes tradeable
    ops.fail_verify.clear()
    await run(store, ops, now=NOW + DAY_MS)
    assert store.records["AAA"].state == "tradeable"


@pytest.mark.asyncio
async def test_watch_only_then_tradeable_after_180_days():
    listed = NOW - 20 * DAY_MS
    store, ops = MemoryUniverseStore(), FakeOps([stat("NEW", 900, onboard=listed)])
    summary = await run(store, ops)
    rec = store.records["NEW"]
    assert rec.state == "watch_only" and rec.data_verified and not rec.warmed_up
    assert ops.names("download") == ["NEW"] and ops.names("warm_up") == []
    assert summary["new_watch_only"] == ["NEW"]
    assert not (await can_open_new_entry("NEW", store, ["NEW"]))[0]

    await run(store, ops, now=NOW + 100 * DAY_MS)  # 120 days old: still watch_only
    assert store.records["NEW"].state == "watch_only"

    later = listed + 180 * DAY_MS + 1
    summary = await run(store, ops, now=later)
    rec = store.records["NEW"]
    assert rec.state == "tradeable" and rec.warmed_up
    assert summary["promoted"] == ["NEW"]
    assert (await can_open_new_entry("NEW", store, ["NEW"]))[0]


@pytest.mark.asyncio
async def test_exited_symbol_keeps_data_and_gets_no_new_entries():
    store = MemoryUniverseStore()
    ops = FakeOps([stat("AAA", 900), stat("BBB", 800)])
    await run(store, ops)
    ops.stats = [stat("AAA", 900)]  # BBB delisted / no longer liquid
    summary = await run(store, ops, now=NOW + DAY_MS)
    rec = store.records["BBB"]
    assert summary["exited"] == ["BBB"]
    assert rec.state == "exited" and not rec.member and rec.exited_at_ms == NOW + DAY_MS
    assert rec.data_verified  # data (and its verification) kept
    assert ops.deleted == [] and "BBB" in ops.have
    ok, why = await can_open_new_entry("BBB", store, ["AAA", "BBB"])
    assert not ok and "exited" in why


@pytest.mark.asyncio
async def test_failed_download_never_blocks_the_rest():
    store = MemoryUniverseStore()
    ops = FakeOps([stat("AAA", 900), stat("BBB", 800), stat("CCC", 700)], fail_download={"BBB"})
    summary = await run(store, ops)
    assert store.records["BBB"].state == "blocked"
    assert store.records["AAA"].state == "tradeable" and store.records["CCC"].state == "tradeable"
    assert summary["downloads_failed"][0]["symbol"] == "BBB"
    assert sorted(summary["downloads_done"]) == ["AAA", "CCC"]
    assert ops.max_in_flight <= CFG.download_concurrency


@pytest.mark.asyncio
async def test_disk_guard_stops_downloads_and_alerts():
    store = MemoryUniverseStore()
    ops = FakeOps([stat("AAA", 900), stat("BBB", 800)], used_pct=95.0)
    summary = await run(store, ops)
    assert ops.names("download") == []
    assert all(store.records[s].state == "blocked" for s in ("AAA", "BBB"))
    assert "disk guard" in store.records["AAA"].reason
    assert sum("disk guard" in a for a in summary["alerts"]) == 1
    ops.used_pct = 40.0  # space freed: the next run downloads them
    await run(store, ops, now=NOW + DAY_MS)
    assert sorted(ops.names("download")) == ["AAA", "BBB"]


@pytest.mark.asyncio
async def test_daily_summary_content_is_stored():
    store = MemoryUniverseStore()
    ops = FakeOps([stat("AAA", 900), stat("NEW", 800, onboard=NOW - 5 * DAY_MS)],
                  fail_download={"NEW"})  # fmt: skip
    summary = await run(store, ops)
    keys = ["run_date", "entered", "exited", "new_watch_only", "downloads_done"]
    keys += ["downloads_failed", "verify_failed", "alerts", "disk", "by_state", "tradeable"]
    for key in keys:
        assert key in summary, key
    assert summary["run_date"] == "2026-09-28"
    assert summary["disk"]["used_pct"] == 50.0 and summary["disk"]["data_dir_bytes"] == 5 * 10**9
    assert summary["tradeable"] == ["AAA"]
    assert summary["by_state"] == {"tradeable": 1, "blocked": 1}
    assert (await store.runs())[0]["entered"] == ["AAA", "NEW"]


@pytest.mark.asyncio
async def test_ranking_failure_changes_nothing():
    class Broken(FakeOps):
        async def fetch_stats(self, include, now_ms):
            raise RuntimeError("exchangeInfo timeout")

    store = MemoryUniverseStore()
    await run(store, FakeOps([stat("AAA", 900)]))
    summary = await run(store, Broken([]), now=NOW + DAY_MS)
    assert store.records["AAA"].state == "tradeable"
    assert "ranking failed" in summary["alerts"][0]


@pytest.mark.asyncio
async def test_gate_needs_trade_symbols_and_state():
    store = MemoryUniverseStore()
    assert not (await can_open_new_entry("AAA", store, ["AAA"]))[0]  # never ran
    await run(store, FakeOps([stat("AAA", 900)]))
    assert (await can_open_new_entry("AAA", store, ["AAA"]))[0]
    ok, why = await can_open_new_entry("AAA", store, [])
    assert not ok and "TRADE_SYMBOLS" in why


def test_scheduler_targets_0001_utc():
    assert seconds_until_next_run(datetime(2026, 9, 27, 23, 0, tzinfo=UTC)) == 3660
    assert seconds_until_next_run(datetime(2026, 9, 28, 0, 1, tzinfo=UTC)) == 86400


@pytest.mark.asyncio
async def test_daily_job_records_point_in_time_membership_and_delisting():
    from deskmind.universe.history import ms_day

    store = MemoryUniverseStore()
    ops = FakeOps([stat("AAA", 900), stat("BBB", 800), stat("NEW", 700, onboard=NOW - DAY_MS)])
    await run(store, ops)
    hist = await store.load_history(ms_day(NOW), ms_day(NOW))
    assert hist.get_universe(ms_day(NOW)) == {"AAA", "BBB"}  # NEW is watch_only that day
    assert hist.get_universe(ms_day(NOW), include_watch_only=True) == {"AAA", "BBB", "NEW"}

    ops.stats = [stat("AAA", 900), stat("NEW", 700, onboard=NOW - DAY_MS)]  # BBB not TRADING
    tomorrow = NOW + DAY_MS
    summary = await run(store, ops, now=tomorrow)
    assert summary["delisted"] == ["BBB"]
    assert store.delistings == {"BBB": tomorrow}
    today_hist = await store.load_history(ms_day(tomorrow), ms_day(tomorrow))
    assert "BBB" not in today_hist.get_universe(ms_day(tomorrow), include_watch_only=True)
    # yesterday's history still has it (point-in-time)
    assert "BBB" in (await store.load_history(ms_day(NOW), ms_day(NOW))).get_universe(ms_day(NOW))
