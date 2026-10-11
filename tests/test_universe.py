"""Daily universe job: ranking, lifecycle, gating, disk guard, summary. All mocked, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from deskmind.universe.config import UniverseConfig
from deskmind.universe.job import DiskUsage, UniverseJob, VerifyOutcome
from deskmind.universe.lifecycle import (
    BlockedReason,
    SymbolState,
    allows_new_entries,
    chart_visible,
    is_tradeable,
)
from deskmind.universe.ranking import MarketSnapshot, select_universe
from deskmind.universe.scheduler import next_run_at
from deskmind.universe.store import RUN_COMPLETED, InMemoryUniverseStore

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=1000)


def cfg(**kw: Any) -> UniverseConfig:
    base: dict[str, Any] = dict(
        top_n=3,
        min_quote_volume_usdt=Decimal("1000"),
        exit_rank_buffer=0,
        min_listing_age_days=180,
        history_days=365,
        warmup_days=84,
        download_concurrency=2,
        disk_max_used_pct=Decimal("90"),
    )
    base.update(kw)
    return UniverseConfig(**base)


def snapshot(volumes: dict[str, str], onboard: dict[str, datetime] | None = None) -> MarketSnapshot:
    onboard = onboard or {}
    symbols = [
        {
            "symbol": s,
            "contractType": "PERPETUAL",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "onboardDate": int(onboard.get(s, OLD).timestamp() * 1000),
        }
        for s in volumes
    ]
    tickers = [{"symbol": s, "quoteVolume": v} for s, v in volumes.items()]
    return MarketSnapshot({"symbols": symbols}, tickers)


class FakeOps:
    def __init__(self) -> None:
        self.history: dict[str, date] = {}  # symbol -> stored start
        self.downloads: list[tuple[str, date]] = []
        self.updates: list[str] = []
        self.verified: list[str] = []
        self.fail_download: set[str] = set()
        self.fail_verify: set[str] = set()
        self.used_pct = 50

    def has_history(self, symbol: str, start: date) -> bool:
        return symbol in self.history and self.history[symbol] <= start

    async def download(self, symbol: str, start: date) -> None:
        if symbol in self.fail_download:
            raise RuntimeError("bulk zip checksum mismatch")
        self.downloads.append((symbol, start))
        self.history[symbol] = start

    async def update(self, symbol: str) -> None:
        if symbol in self.fail_download:
            raise RuntimeError("network down")
        self.updates.append(symbol)

    async def verify(self, symbol: str) -> VerifyOutcome:
        self.verified.append(symbol)
        if symbol in self.fail_verify:
            return VerifyOutcome("FAIL", ["klines/5m: duplicates: 3"])
        return VerifyOutcome("PASS")

    def disk_usage(self) -> DiskUsage:
        total = 1000
        used = total * self.used_pct // 100
        return DiskUsage(total, used, total - used, 42)


class Harness:
    def __init__(self, c: UniverseConfig | None = None) -> None:
        self.store = InMemoryUniverseStore()
        self.ops = FakeOps()
        self.snap = snapshot({})
        self.warmed: list[str] = []
        self.notified: list[dict[str, Any]] = []

        async def market() -> MarketSnapshot:
            return self.snap

        async def warm(symbol: str) -> int:
            self.warmed.append(symbol)
            return 5

        async def notify(summary: dict[str, Any]) -> None:
            self.notified.append(summary)

        self.job = UniverseJob(c or cfg(), self.store, market, self.ops, warm, notify=notify)

    async def run(self, snap: MarketSnapshot, now: datetime = NOW) -> dict[str, Any]:
        self.snap = snap
        return await self.job.run(now)


# ---------------------------------------------------------------- ranking


def test_ranking_is_liquidity_only_and_filters_contracts() -> None:
    snap = snapshot({"AAAUSDT": "5000", "BBBUSDT": "9000", "CCCUSDT": "7000", "DDDUSDT": "10"})
    snap.exchange_info["symbols"].append(
        {
            "symbol": "EEEUSDT",
            "contractType": "CURRENT_QUARTER",
            "status": "TRADING",
            "quoteAsset": "USDT",
        }
    )
    snap.tickers_24h.append({"symbol": "EEEUSDT", "quoteVolume": "99999"})
    sel = select_universe(snap, set(), cfg())
    assert list(sel.members) == ["BBBUSDT", "CCCUSDT", "AAAUSDT"]  # by 24h quote volume
    assert "EEEUSDT" not in sel.ranked  # not a perpetual
    assert "DDDUSDT" not in sel.members  # under the volume floor


def test_exit_buffer_keeps_member_near_the_cut() -> None:
    snap = snapshot({"AAUSDT": "9000", "BBUSDT": "8000", "CCUSDT": "7000", "DDUSDT": "6000"})
    sel = select_universe(snap, {"DDUSDT"}, cfg(exit_rank_buffer=1))
    assert set(sel.members) == {"DDUSDT", "AAUSDT", "BBUSDT"}
    sel = select_universe(snap, {"DDUSDT"}, cfg(exit_rank_buffer=0))
    assert "DDUSDT" not in sel.members


# ---------------------------------------------------------------- lifecycle


async def test_entry_triggers_download_verify_warmup_then_tradeable() -> None:
    h = Harness()
    out = await h.run(snapshot({"AAUSDT": "5000"}))
    assert h.ops.downloads == [("AAUSDT", NOW.date() - timedelta(days=365 + 84))]
    assert h.ops.verified == ["AAUSDT"] and h.warmed == ["AAUSDT"]
    rec = h.store.records["AAUSDT"]
    assert rec.state == SymbolState.tradeable and is_tradeable(rec) and allows_new_entries(rec)
    assert [t["to"] for t in rec.history] == ["downloading", "verifying", "warming_up", "tradeable"]
    assert out["entered"][0]["symbol"] == "AAUSDT"
    assert out["downloads"]["done"] == [
        {"symbol": "AAUSDT", "kind": "history", "start": h.ops.downloads[0][1].isoformat()}
    ]


async def test_download_never_starts_before_listing() -> None:
    h = Harness()
    listed = NOW - timedelta(days=200)
    await h.run(snapshot({"AAUSDT": "5000"}, {"AAUSDT": listed}))
    assert h.ops.downloads == [("AAUSDT", listed.date() + timedelta(days=1))]


async def test_existing_member_only_updates() -> None:
    h = Harness()
    await h.run(snapshot({"AAUSDT": "5000"}))
    h.ops.downloads.clear(), h.ops.verified.clear(), h.warmed.clear()
    out = await h.run(snapshot({"AAUSDT": "6000"}), NOW + timedelta(days=1))
    assert h.ops.updates == ["AAUSDT"]
    assert h.ops.downloads == [] and h.ops.verified == [] and h.warmed == []
    assert h.store.records["AAUSDT"].state == SymbolState.tradeable
    assert out["entered"] == [] and out["downloads"]["done"][0]["kind"] == "update"


async def test_returning_symbol_with_data_updates_instead_of_downloading() -> None:
    h = Harness()
    h.ops.history["AAUSDT"] = date(2020, 1, 1)
    await h.run(snapshot({"AAUSDT": "5000"}))
    assert h.ops.downloads == [] and h.ops.updates == ["AAUSDT"]
    assert h.ops.verified == ["AAUSDT"]  # still verified + warmed up before trading
    assert is_tradeable(h.store.records["AAUSDT"])


async def test_verification_failure_blocks_trading_and_alerts() -> None:
    h = Harness()
    h.ops.fail_verify.add("AAUSDT")
    out = await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000"}))
    rec = h.store.records["AAUSDT"]
    assert rec.state == SymbolState.blocked
    assert rec.blocked_reason == BlockedReason.verification_failed
    assert not is_tradeable(rec) and not allows_new_entries(rec)
    assert "AAUSDT" not in h.warmed  # never warmed up on bad data
    assert is_tradeable(h.store.records["BBUSDT"])  # the rest carries on
    alerts = [(a["kind"], a["symbol"]) for a in out["alerts"]]
    assert ("verification_failed", "AAUSDT") in alerts
    assert out["blocked"][0]["symbol"] == "AAUSDT"

    # stays blocked while verification keeps failing; recovers once it passes
    await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000"}), NOW + timedelta(days=1))
    assert h.store.records["AAUSDT"].state == SymbolState.blocked
    h.ops.fail_verify.clear()
    out = await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000"}), NOW + timedelta(days=2))
    assert is_tradeable(h.store.records["AAUSDT"]) and out["retried"] == ["AAUSDT"]


async def test_failed_download_does_not_block_the_rest() -> None:
    h = Harness()
    h.ops.fail_download.add("AAUSDT")
    out = await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000", "CCUSDT": "3000"}))
    assert h.store.records["AAUSDT"].blocked_reason == BlockedReason.download_failed
    assert is_tradeable(h.store.records["BBUSDT"]) and is_tradeable(h.store.records["CCUSDT"])
    assert [f["symbol"] for f in out["downloads"]["failed"]] == ["AAUSDT"]
    assert out["status"] == RUN_COMPLETED


async def test_watch_only_becomes_tradeable_after_180_days() -> None:
    h = Harness()
    listed = NOW - timedelta(days=30)
    snap = snapshot({"NEWUSDT": "5000"}, {"NEWUSDT": listed})
    out = await h.run(snap)
    rec = h.store.records["NEWUSDT"]
    assert rec.state == SymbolState.watch_only and not allows_new_entries(rec)
    assert chart_visible(rec) and h.ops.downloads  # data collected for the chart
    assert h.ops.verified == [] and h.warmed == []
    assert out["new_watch_only"] == ["NEWUSDT"]

    out = await h.run(snap, listed + timedelta(days=179))
    assert h.store.records["NEWUSDT"].state == SymbolState.watch_only

    out = await h.run(snap, listed + timedelta(days=181))
    rec = h.store.records["NEWUSDT"]
    assert rec.state == SymbolState.tradeable and is_tradeable(rec)
    assert out["promoted"] == ["NEWUSDT"] and h.ops.verified == ["NEWUSDT"]


async def test_exited_symbol_keeps_data_and_gets_no_new_entries() -> None:
    h = Harness()
    await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000", "CCUSDT": "3000"}))
    out = await h.run(
        snapshot({"AAUSDT": "5000", "BBUSDT": "4000", "CCUSDT": "3000", "DDUSDT": "9000"}),
        NOW + timedelta(days=1),
    )
    rec = h.store.records["CCUSDT"]
    assert out["exited"] == ["CCUSDT"]
    assert rec.state == SymbolState.exited and not rec.in_universe
    assert not allows_new_entries(rec) and chart_visible(rec)
    assert "CCUSDT" in h.ops.history  # data kept


async def test_disk_guard_stops_downloads_and_alerts() -> None:
    h = Harness()
    h.ops.used_pct = 95
    out = await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000"}))
    assert h.ops.downloads == []
    assert all(r.blocked_reason == BlockedReason.disk_guard for r in h.store.records.values())
    assert out["disk"]["guard_tripped"] is True
    assert sorted(out["skipped_disk_guard"]) == ["AAUSDT", "BBUSDT"]
    assert [a["kind"] for a in out["alerts"]].count("disk_guard") == 1


async def test_daily_summary_content_and_run_once_per_day() -> None:
    h = Harness()
    await h.run(snapshot({"AAUSDT": "5000", "BBUSDT": "4000"}))
    young = NOW - timedelta(days=10)
    out = await h.run(
        snapshot({"AAUSDT": "5000", "NEWUSDT": "4500", "CCUSDT": "3000"}, {"NEWUSDT": young}),
        NOW + timedelta(days=1),
    )
    assert out["status"] == RUN_COMPLETED and out["run_date"] == "2026-10-02"
    assert [e["symbol"] for e in out["entered"]] == ["NEWUSDT", "CCUSDT"]
    assert out["exited"] == ["BBUSDT"]
    assert out["new_watch_only"] == ["NEWUSDT"]
    assert {d["symbol"] for d in out["downloads"]["done"]} == {"AAUSDT", "NEWUSDT", "CCUSDT"}
    assert out["downloads"]["failed"] == []
    assert out["universe_size"] == 3 and out["tradeable"] == ["AAUSDT", "CCUSDT"]
    assert out["disk"]["used_pct"] == "50.00" and out["disk"]["data_dir_bytes"] == 42
    assert out["disk"]["threshold_pct"] == "90" and out["disk"]["guard_tripped"] is False
    assert h.notified[-1] == out  # handed to the notifier (Telegram later)

    again = await h.run(snapshot({"AAUSDT": "1"}), NOW + timedelta(days=1, hours=3))
    assert again == {"run_date": "2026-10-02", "skipped": True}
    assert (await h.store.recent_runs(1))[0]["summary"] == out


async def test_market_failure_marks_run_failed_without_touching_symbols() -> None:
    h = Harness()
    await h.run(snapshot({"AAUSDT": "5000"}))

    async def broken() -> MarketSnapshot:
        raise RuntimeError("exchangeInfo 503")

    h.job.market = broken
    out = await h.job.run(NOW + timedelta(days=1))
    assert out["status"] == "failed" and out["alerts"][0]["kind"] == "run_failed"
    assert is_tradeable(h.store.records["AAUSDT"])


def test_next_run_is_midnight_utc() -> None:
    assert next_run_at(datetime(2026, 10, 1, 13, 5, tzinfo=UTC), 0) == datetime(
        2026, 10, 2, tzinfo=UTC
    )
    assert next_run_at(datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 0) == datetime(
        2026, 10, 2, tzinfo=UTC
    )


# ---------------------------------------------------------------- dashboard endpoint


@pytest.fixture
def client(tmp_path, monkeypatch):
    import fakeredis
    from fastapi.testclient import TestClient

    import deskmind.api.dashboard as dash
    from deskmind.api.app import create_app
    from deskmind.config import Settings

    store = InMemoryUniverseStore()
    monkeypatch.setattr(dash, "universe_store", lambda: store)
    settings = Settings(_env_file=None, postgres_password="x", data_dir=tmp_path)
    app = create_app(
        settings, redis=fakeredis.FakeAsyncRedis(decode_responses=True), health_checks=False
    )
    return TestClient(app), store


async def test_dashboard_universe_shows_stored_states(client) -> None:
    tc, store = client
    body = tc.get("/api/dashboard/universe").json()
    assert body["symbols"] == [] and body["runs"] == [] and body["reason"]

    h = Harness()
    h.store = store
    h.job.store = store
    await h.run(snapshot({"AAUSDT": "5000"}))
    body = tc.get("/api/dashboard/universe").json()
    assert body["symbols"][0]["symbol"] == "AAUSDT"
    assert body["symbols"][0]["state"] == "tradeable" and body["symbols"][0]["tradeable"]
    assert body["runs"][0]["summary"]["entered"][0]["symbol"] == "AAUSDT"
    assert body["reason"] is None
    assert "tradeable" in body["states"] and body["config"]["top_n"] > 0
