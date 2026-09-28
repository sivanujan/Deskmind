"""Point-in-time universe: history builder, get_universe, backtest filter and delisting.

All mocked: synthetic volumes, fake S3 listing, synthetic Parquet in tmp_path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from deskmind.backtest.engine import BacktestEngine
from deskmind.backtest.sizing import FixedFractionalSizer
from deskmind.strategies.base import Intent
from deskmind.universe.history import (
    MembershipHistory,
    MembershipRow,
    compute_membership,
    day_ms,
    find_delistings,
    parse_s3_listing,
)
from deskmind.universe.ranking import UniverseConfig
from deskmind.universe.store import MemoryUniverseStore, get_universe
from tests.test_backtest import ScriptedStrategy, _setup_symbol_data

CFG = UniverseConfig(top_n=2, exit_rank=3, lookback_days=3, min_listing_age_days=10)
D0 = date(2024, 1, 1)


def days(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


# ---------------------------------------------------------------- history builder


def test_membership_uses_only_prior_days_no_look_ahead():
    vols = {
        "AAAUSDT": dict.fromkeys(days(20), 100.0),
        "BBBUSDT": dict.fromkeys(days(20), 50.0),
        "CCCUSDT": dict.fromkeys(days(20), 10.0),
    }
    # CCC becomes the most liquid from day 15 on. With a 3-day median it needs two closed
    # high-volume days (15, 16), so it can enter on day 17 at the earliest; using the (not yet
    # closed) candle of the day itself would let it in on day 16.
    for d in days(5, D0 + timedelta(days=15)):
        vols["CCCUSDT"][d] = 10_000.0
    first = dict.fromkeys(vols, D0 - timedelta(days=100))
    rows = compute_membership(vols, D0 + timedelta(days=12), D0 + timedelta(days=19), CFG, first)
    by_day: dict[date, set[str]] = {}
    for r in rows:
        by_day.setdefault(r.day, set()).add(r.symbol)
    assert "CCCUSDT" not in by_day[D0 + timedelta(days=15)]
    assert "CCCUSDT" not in by_day[D0 + timedelta(days=16)]
    assert "CCCUSDT" in by_day[D0 + timedelta(days=17)]


def test_delisted_symbol_in_history_but_not_after_delisting():
    vols = {
        "AAAUSDT": dict.fromkeys(days(30), 100.0),
        "DEADUSDT": dict.fromkeys(days(15), 500.0),  # last candle on day 14, then delisted
        "BBBUSDT": dict.fromkeys(days(30), 50.0),
    }
    first = dict.fromkeys(vols, D0 - timedelta(days=100))
    rows = compute_membership(vols, D0 + timedelta(days=5), D0 + timedelta(days=25), CFG, first)
    hist = MembershipHistory(rows)
    assert "DEADUSDT" in hist.get_universe(D0 + timedelta(days=10))
    assert "DEADUSDT" in hist.get_universe(D0 + timedelta(days=15))  # decided from day 14
    assert "DEADUSDT" not in hist.get_universe(D0 + timedelta(days=16))
    delisted = find_delistings(vols, trading_now={"AAAUSDT", "BBBUSDT"}, today=D0 + timedelta(30))
    assert delisted == {"DEADUSDT": day_ms(D0 + timedelta(days=15))}
    assert "AAAUSDT" not in delisted  # still trading today


def test_young_listing_is_member_but_not_tradeable():
    vols = {"AAAUSDT": dict.fromkeys(days(20), 100.0), "NEWUSDT": {}}
    for d in days(8, D0 + timedelta(days=12)):
        vols["NEWUSDT"][d] = 900.0
    first = {"AAAUSDT": D0 - timedelta(days=100), "NEWUSDT": D0 + timedelta(days=12)}
    rows = compute_membership(vols, D0 + timedelta(days=15), D0 + timedelta(days=19), CFG, first)
    hist = MembershipHistory(rows)
    d = D0 + timedelta(days=15)
    assert "NEWUSDT" in hist.get_universe(d, include_watch_only=True)
    assert "NEWUSDT" not in hist.get_universe(d)  # watch_only that day


def test_missing_day_means_nobody():
    hist = MembershipHistory([MembershipRow(D0, "AAAUSDT", 1, 1.0, True)])
    assert hist.get_universe(D0) == {"AAAUSDT"}
    assert hist.get_universe(D0 + timedelta(days=1)) == set()
    assert not hist.covers(D0, D0 + timedelta(days=1))
    assert hist.missing_days(D0, D0 + timedelta(days=1)) == [D0 + timedelta(days=1)]


def test_parse_s3_listing_keeps_usdt_perpetuals_and_paginates():
    xml = (
        "<ListBucketResult><IsTruncated>true</IsTruncated>"
        "<CommonPrefixes><Prefix>data/futures/um/monthly/klines/1000BONKUSDT/</Prefix>"
        "</CommonPrefixes><CommonPrefixes><Prefix>data/futures/um/monthly/klines/"
        "BTCUSDT_240628/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>"
        "data/futures/um/monthly/klines/ETHBTC/</Prefix></CommonPrefixes>"
        "<NextMarker>data/futures/um/monthly/klines/ETHBTC/</NextMarker></ListBucketResult>"
    )
    symbols, marker = parse_s3_listing(xml)
    assert symbols == ["1000BONKUSDT"]
    assert marker == "data/futures/um/monthly/klines/ETHBTC/"
    last, none = parse_s3_listing("<IsTruncated>false</IsTruncated>")
    assert last == [] and none is None


@pytest.mark.asyncio
async def test_get_universe_reads_point_in_time_table():
    store = MemoryUniverseStore()
    await store.save_membership(
        [MembershipRow(D0, "AAAUSDT", 1, 9.0, True), MembershipRow(D0, "NEWUSDT", 2, 8.0, False)],
        source="backfill",
    )
    assert await get_universe(D0, store) == {"AAAUSDT"}
    assert await get_universe(D0 + timedelta(days=1), store) == set()


# ---------------------------------------------------------------- backtest engine

T0 = int(datetime(2024, 1, 1, 0, 0, tzinfo=UTC).timestamp() * 1000)
STEP = 300_000


def bar(t: int, o: float, h: float, lo: float, c: float) -> dict:
    return {"open_time": t, "close_time": t + STEP, "open": o, "high": h, "low": lo, "close": c}


def _engine(tmp_path: Path, strategy, symbols, universe) -> BacktestEngine:
    return BacktestEngine(
        strategy=strategy,
        symbols=symbols,
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        taker_fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("0"),
        slippage_atr_factor=Decimal("0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
        universe=universe,
    )


def _long(sym: str) -> Intent:
    return Intent(
        symbol=sym,
        action="open_long",
        order_type="market",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("120.0"),
    )


@pytest.mark.parametrize(
    ("rows", "entered", "expected_skips"),
    [
        ([MembershipRow(D0, "BTCUSDT", 1, 1.0, True)], True, 0),  # member that day: enters
        ([MembershipRow(D0, "BTCUSDT", 1, 1.0, False)], False, 1),  # watch_only: no entry
        ([], False, 1),  # not a member that day: no entry
    ],
)
def test_backtest_only_enters_symbols_in_that_days_universe(
    tmp_path: Path, rows, entered, expected_skips
):
    klines = [bar(T0 + i * STEP, 100, 101, 99.5, 100) for i in range(6)]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)
    strategy = ScriptedStrategy({T0 + STEP: [_long("BTCUSDT")]})
    engine = _engine(tmp_path, strategy, ["BTCUSDT"], MembershipHistory(rows))
    res = engine.run()
    assert (engine.active_position is not None) is entered  # SL/TP never hit: still open
    assert res.universe_skips == expected_skips
    assert res.universe_filter == "point_in_time"


def test_delisting_with_open_position_settles_at_last_price(tmp_path: Path):
    # SOL trades until bar 2 (open T0+2*STEP) then is delisted; ETH keeps the clock running.
    sol = [
        bar(T0, 100, 101, 99.5, 100),
        bar(T0 + STEP, 100, 102, 99.5, 101),
        bar(T0 + 2 * STEP, 101, 103, 100.5, 102.5),
    ]
    eth = [bar(T0 + i * STEP, 50, 51, 49.5, 50) for i in range(8)]
    _setup_symbol_data(tmp_path, "SOLUSDT", sol)
    _setup_symbol_data(tmp_path, "ETHUSDT", eth)
    delisted_at = T0 + 3 * STEP
    hist = MembershipHistory(
        [MembershipRow(D0, "SOLUSDT", 1, 1.0, True), MembershipRow(D0, "ETHUSDT", 2, 1.0, True)],
        delisted_at_ms={"SOLUSDT": delisted_at},
    )
    strategy = ScriptedStrategy({T0 + STEP: [_long("SOLUSDT")]})
    res = _engine(tmp_path, strategy, ["SOLUSDT", "ETHUSDT"], hist).run()
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr.exit_reason == "delisted"
    assert tr.exit_price == Decimal("102.5")  # last traded price before delisting
    assert tr.exit_time_ms == delisted_at
    assert res.delisted_exits == 1
    fees = tr.entry_price * tr.qty * Decimal("0.0005") + Decimal("102.5") * tr.qty * Decimal(
        "0.0005"
    )
    assert tr.pnl == (Decimal("102.5") - tr.entry_price) * tr.qty - fees
    assert res.final_equity == Decimal("10000.0") + tr.pnl


def test_without_universe_engine_behaves_as_before(tmp_path: Path):
    klines = [bar(T0 + i * STEP, 100, 101, 99.5, 100) for i in range(6)]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)
    strategy = ScriptedStrategy({T0 + STEP: [_long("BTCUSDT")]})
    engine = _engine(tmp_path, strategy, ["BTCUSDT"], None)
    res = engine.run()
    assert res.universe_filter == "none" and res.universe_skips == 0
    assert engine.active_position is not None
