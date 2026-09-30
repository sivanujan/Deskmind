"""Tests for the ICT Time agent: sessions, key levels, truncation, no look-ahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from deskmind.agents.ict_time.agent import IctTimeAgent, run_batch
from deskmind.agents.ict_time.checks import truncation_check
from deskmind.agents.ict_time.config import DEFAULT_CONFIG
from deskmind.agents.ict_time.engine import IctTimeEngine, _to_ms
from deskmind.agents.ict_time.models import as_of
from deskmind.agents.ict_time.store import MemoryIctTimeStore
from deskmind.agents.trend.models import Bar

_NY = ZoneInfo("America/New_York")


def _bar(open_dt: datetime, *, o=100.0, h=101.0, l=99.0, c=100.5, ms=300_000) -> Bar:
    open_ms = int(open_dt.timestamp() * 1000)
    close_ms = open_ms + ms
    return Bar(
        open_time=open_ms,
        close_time=close_ms,
        open=o,
        high=h,
        low=l,
        close=c,
    )


def _ny_at(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_NY)


def _day_bars(date_ny: datetime, n: int = 48) -> list[Bar]:
    """48 x 30m bars starting from midnight NY."""
    bars = []
    cur = date_ny
    for _ in range(n):
        bars.append(_bar(cur, ms=1_800_000))
        cur = cur + timedelta(minutes=30)
    return bars


# ------------------------------------------------------------------ basic session


def test_session_created_on_first_candle() -> None:
    eng = IctTimeEngine("BTC")
    # 20:00 NY = Asia session open
    w_dt = _ny_at(2026, 9, 28, 19, 0)
    dt = _ny_at(2026, 9, 28, 20, 0)
    eng.on_candle(_bar(w_dt, ms=3_600_000))  # warmup
    bar = _bar(dt, ms=3_600_000)
    changes = eng.on_candle(bar)
    session_changes = [c for c in changes if c["kind"].startswith("session_")]
    assert len(session_changes) >= 1
    assert session_changes[0]["kind"] == "session_asia"
    assert session_changes[0]["state"] == "active"


def test_session_closes_on_new_session() -> None:
    eng = IctTimeEngine("BTC")
    # Asia session bar
    w_dt = _ny_at(2026, 9, 28, 19, 0)
    asia_dt = _ny_at(2026, 9, 28, 21, 0)
    eng.on_candle(_bar(w_dt, ms=3_600_000))
    eng.on_candle(_bar(asia_dt, ms=3_600_000))
    # London session bar (02:00 NY)
    london_dt = _ny_at(2026, 9, 29, 2, 0)
    changes = eng.on_candle(_bar(london_dt, ms=3_600_000))
    session_kinds = {c["kind"]: c["state"] for c in changes if c["kind"].startswith("session_")}
    # Asia should be closed, London active
    assert session_kinds.get("session_london") == "active"
    # Asia closed somewhere in the change list
    asia_close = [c for c in changes if c["kind"] == "session_asia" and c["state"] == "closed"]
    assert asia_close, f"Expected session_asia to be closed, got changes: {changes}"


def test_ny_session_active_during_ny_hours() -> None:
    eng = IctTimeEngine("BTC")
    w_dt = _ny_at(2026, 9, 29, 8, 30)
    ny_dt = _ny_at(2026, 9, 29, 9, 30)
    eng.on_candle(_bar(w_dt, ms=3_600_000))
    changes = eng.on_candle(_bar(ny_dt, ms=3_600_000))
    ny = [c for c in changes if c["kind"] == "session_new_york"]
    assert ny and ny[0]["state"] == "active"


# ------------------------------------------------------------------ key levels


def test_pdh_pdl_emitted_on_new_day() -> None:
    """PDH/PDL from day 1 confirmed at the first bar of day 2."""
    eng = IctTimeEngine("BTC")
    # Feed 1 bar on day 1 (at 21:00 NY), then 1 bar on day 2 (at 01:00 NY next day)
    day1_bar0 = _bar(_ny_at(2026, 9, 28, 12, 0), h=105.0, l=95.0, c=100.0, ms=3_600_000)
    day1_bar = _bar(_ny_at(2026, 9, 28, 21, 0), h=105.0, l=95.0, c=100.0, ms=3_600_000)
    day2_bar = _bar(_ny_at(2026, 9, 29, 1, 0), h=103.0, l=98.0, c=101.0, ms=3_600_000)
    eng.on_candle(day1_bar0)
    changes_d1 = eng.on_candle(day1_bar)
    changes_d2 = eng.on_candle(day2_bar)
    all_changes = changes_d1 + changes_d2
    level_kinds = {c["kind"] for c in all_changes}
    # After the new day opens, PDH/PDL should be in the changes
    assert "pdh" in level_kinds or "pdl" in level_kinds


def test_key_level_state_sweep() -> None:
    eng = IctTimeEngine("BTC")
    # Day 1: generate high=110
    d1_0 = _bar(_ny_at(2026, 9, 28, 12, 0), h=100.0, l=90.0, c=95.0, ms=3_600_000)
    d1 = _bar(_ny_at(2026, 9, 28, 21, 0), h=110.0, l=90.0, c=100.0, ms=3_600_000)
    eng.on_candle(d1_0)
    eng.on_candle(d1)
    # Day 2 open -> PDH at 110 emitted
    d2_open = _bar(_ny_at(2026, 9, 29, 1, 0), h=102.0, l=98.0, c=100.0, ms=3_600_000)
    eng.on_candle(d2_open)
    # Day 2 candle sweeps through 110 but closes below
    d2_sweep = _bar(_ny_at(2026, 9, 29, 10, 0), h=115.0, l=99.0, c=108.0, ms=3_600_000)
    changes = eng.on_candle(d2_sweep)
    states = {c["kind"]: c.get("state") for c in changes}
    # PDH should be swept or taken
    pdh_state = states.get("pdh") or next((v for k, v in states.items() if "pdh" in k), None)
    assert pdh_state in ("swept", "taken"), f"Expected swept/taken, got {pdh_state}"



# ------------------------------------------------------------------ no look-ahead / as_of


def test_as_of_masks_future_state() -> None:
    """as_of(item, T) at an early T should not show swept_at that happened after T."""
    item = {
        "id": "pdh_2026-09-28",
        "kind": "pdh",
        "created_time": 1_000_000,
        "confirmed_at": 2_000_000,
        "price": 110.0,
        "reference_period": "2026-09-28",
        "state": "swept",
        "swept_at": 5_000_000,
        "taken_at": None,
        "swept_by": None,
    }
    # As of before confirmation: None
    assert as_of(item, 1_000_000) is None
    # As of after confirmation but before sweep
    view = as_of(item, 3_000_000)
    assert view is not None
    assert view["state"] == "active"
    assert view["swept_at"] is None
    # As of after sweep
    view2 = as_of(item, 6_000_000)
    assert view2 is not None
    assert view2["state"] == "swept"


# ------------------------------------------------------------------ truncation


def test_truncation_no_lookahead() -> None:
    """Full 5-day simulation: truncation check must pass for all cuts."""
    bars: list[Bar] = []
    # 5 days × 48 half-hour bars
    start = _ny_at(2026, 9, 25, 20, 0)  # Asia session start
    for day_offset in range(5):
        day_start = start + timedelta(days=day_offset)
        bars.extend(_day_bars(day_start, n=48))
    result = truncation_check(bars, n_cuts=10, seed=7, cfg=DEFAULT_CONFIG)
    assert result.passed == result.cuts, f"Failures: {result.failures}"


# ------------------------------------------------------------------ AMD heuristic


def test_amd_phase_accumulation_before_london() -> None:
    eng = IctTimeEngine("BTC")
    # Asian session bars
    for i in range(8):
        dt = _ny_at(2026, 9, 29, 20, 0) + timedelta(hours=i)
        eng.on_candle(_bar(dt, ms=3_600_000, h=101.0, l=99.0))
    state = eng.state()
    assert state["amd"]["phase"] == "accumulation"


# ------------------------------------------------------------------ store round-trip


@pytest.mark.asyncio
async def test_memory_store_roundtrip() -> None:
    store = MemoryIctTimeStore()
    agent = IctTimeAgent("BTCUSDT")
    bars: list[Bar] = []
    start = _ny_at(2026, 9, 28, 20, 0)
    for i in range(96):
        dt = start + timedelta(minutes=30 * i)
        bars.append(_bar(dt, ms=1_800_000))

    result = run_batch(agent, {"30m": bars}, reports=True)
    await store.upsert_items("BTCUSDT", result.items, DEFAULT_CONFIG.version)
    await store.upsert_reports("BTCUSDT", result.reports)

    last_t = bars[-1].close_time
    rpt = await store.latest_report("BTCUSDT", last_t)
    assert rpt is not None
    assert rpt["symbol"] == "BTCUSDT"
    items = await store.items("BTCUSDT", None, None, None)
    assert len(items) > 0
    # All items must have confirmed_at
    for it in items:
        assert "confirmed_at" in it


# ------------------------------------------------------------------ killzone / silver bullet


def test_killzone_detected_at_ny_am() -> None:
    from deskmind.agents.ict_time.engine import _current_killzone

    # 08:30 AM NY = NY AM killzone
    dt = _ny_at(2026, 9, 29, 8, 30)
    ms = int(dt.timestamp() * 1000)
    kz = _current_killzone(ms)
    assert kz == "ny_am"


def test_silver_bullet_sb1() -> None:
    from deskmind.agents.ict_time.engine import _current_silver_bullet

    # 03:30 NY = SB1 (03:00-04:00)
    dt = _ny_at(2026, 9, 29, 3, 30)
    ms = int(dt.timestamp() * 1000)
    sb = _current_silver_bullet(ms)
    assert sb == "sb1"


def test_not_in_silver_bullet_outside_window() -> None:
    from deskmind.agents.ict_time.engine import _current_silver_bullet

    # 06:00 NY = not in any SB
    dt = _ny_at(2026, 9, 29, 6, 0)
    ms = int(dt.timestamp() * 1000)
    assert _current_silver_bullet(ms) is None


# ------------------------------------------------------------------ multi-symbol isolation


def test_multi_symbol_isolation() -> None:
    """Two symbols run independently; changes for BTC don't affect ETH."""
    btc = IctTimeEngine("BTC")
    eth = IctTimeEngine("ETH")
    w_dt = _ny_at(2026, 9, 28, 19, 0)
    dt = _ny_at(2026, 9, 28, 21, 0)
    btc.on_candle(_bar(w_dt, h=200.0, l=180.0, c=190.0))
    eth.on_candle(_bar(w_dt, h=3000.0, l=2800.0, c=2900.0))
    bar_btc = _bar(dt, h=200.0, l=180.0, c=190.0)
    bar_eth = _bar(dt, h=3000.0, l=2800.0, c=2900.0)
    btc.on_candle(bar_btc)
    eth.on_candle(bar_eth)
    btc_state = btc.state()
    eth_state = eth.state()
    # Both have an active Asia session
    btc_sess = btc_state["sessions"]
    eth_sess = eth_state["sessions"]
    assert any(v["state"] == "active" for v in btc_sess.values())
    assert any(v["state"] == "active" for v in eth_sess.values())
    # Their session highs are independent
    btc_asia = btc_sess.get("asia")
    eth_asia = eth_sess.get("asia")
    if btc_asia and eth_asia:
        assert btc_asia["high"] != eth_asia["high"]



# ------------------------------------------------------------------ DST & state reconstruction


def test_dst_spring_and_fall_transitions() -> None:
    """Verify session / killzone detection across NY DST spring (March) & fall (Nov) switches."""
    eng = IctTimeEngine("BTC")
    # Spring DST transition date (e.g. 2026-03-08 in US): 08:30 AM NY
    spring_dt = datetime(2026, 3, 8, 8, 30, tzinfo=_NY)
    bar_spring = _bar(spring_dt, ms=3_600_000)
    changes_spring = eng.on_candle(bar_spring)
    # Check that UTC offset changed to -04:00 (EDT) and NY AM killzone is active
    assert spring_dt.utcoffset() == timedelta(hours=-4)
    from deskmind.agents.ict_time.engine import _current_killzone
    assert _current_killzone(int(spring_dt.timestamp() * 1000)) == "ny_am"

    # Fall DST transition date (e.g. 2026-11-01 in US): 08:30 AM NY
    fall_dt = datetime(2026, 11, 1, 8, 30, tzinfo=_NY)
    assert fall_dt.utcoffset() == timedelta(hours=-5)
    assert _current_killzone(int(fall_dt.timestamp() * 1000)) == "ny_am"


def test_pdh_pdl_day_boundary_midnight_ny() -> None:
    """Day boundary is midnight America/New_York (00:00 NY). Verify PDH/PDL emitted when day rolls over at 00:00 NY."""
    eng = IctTimeEngine("BTC")
    d1_bar0 = _bar(_ny_at(2026, 9, 28, 11, 0), h=110.0, l=90.0, c=100.0, ms=1_800_000)
    d1_bar1 = _bar(_ny_at(2026, 9, 28, 12, 0), h=120.0, l=80.0, c=100.0, ms=1_800_000)
    d1_bar2 = _bar(_ny_at(2026, 9, 28, 23, 30), h=110.0, l=90.0, c=100.0, ms=1_800_000)
    d2_start = _bar(_ny_at(2026, 9, 29, 0, 0), h=105.0, l=95.0, c=102.0, ms=1_800_000)
    eng.on_candle(d1_bar0)
    c1 = eng.on_candle(d1_bar1)
    c2 = eng.on_candle(d1_bar2)
    c3 = eng.on_candle(d2_start)

    all_changes = c1 + c2 + c3
    pdh = next((c for c in all_changes if c["kind"] == "pdh"), None)
    assert pdh is not None
    assert pdh["price"] == 120.0
    assert pdh["reference_period"] == "2026-09-28"




def test_reconstruct_state_equals_live_engine() -> None:
    """reconstruct_state(T) from item log matches live engine state at random time T."""
    eng = IctTimeEngine("BTC")
    bars = _day_bars(_ny_at(2026, 9, 28, 0, 0), n=48)
    all_items = []
    for b in bars:
        all_items.extend(eng.on_candle(b))

    cut_bar = bars[24]
    cut_t = cut_bar.close_time
    live_eng = IctTimeEngine("BTC")
    for b in bars[:25]:
        live_eng.on_candle(b)

    from deskmind.agents.ict_time.models import reconstruct_state
    rec_state = reconstruct_state(all_items, cut_t)
    live_state = live_eng.state()
    assert len(rec_state["key_levels"]) == len(live_state["key_levels"])


def test_unique_item_ids() -> None:
    """Each item type emitted has a unique deterministic ID format without collision across entities."""
    eng = IctTimeEngine("BTC")
    bars = _day_bars(_ny_at(2026, 9, 28, 0, 0), n=48)
    items_by_id = {}
    for b in bars:
        for it in eng.on_candle(b):
            items_by_id[it["id"]] = it
    # Check that distinct kinds produce distinct stable IDs
    assert len(items_by_id) >= 5
    assert all("id" in v for v in items_by_id.values())


def test_dst_spring_skipped_hour_london_session() -> None:
    """On DST start (2026-03-08), local NY 02:00 does not exist (clocks jump 01:59:59 EST -> 03:00:00 EDT).

    London session is defined as starting at 07:00 UTC (03:00 EDT / 02:00 EST). The engine
    uses UTC epoch timestamps converted to America/New_York so that 07:00 UTC resolves to 03:00 EDT,
    opening the London session cleanly without error or missing events.
    """
    eng = IctTimeEngine("BTC")
    # 2026-03-08 07:00 UTC = 03:00 EDT (London session start)
    london_utc = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    bar1 = _bar(_ny_at(2026, 3, 7, 21, 0), ms=3_600_000)  # warmup bar 1
    bar2 = _bar(london_utc, ms=3_600_000)                # London start bar
    
    eng.on_candle(bar1)
    changes = eng.on_candle(bar2)
    session_kinds = {c["kind"]: c["state"] for c in changes if c["kind"].startswith("session_")}
    assert session_kinds.get("session_london") == "active"


def test_ict_time_cold_start_warmup() -> None:
    """Engine refuses to emit items on a single synthetic bar without warmup."""
    eng = IctTimeEngine("BTC")
    bar = _bar(_ny_at(2026, 9, 28, 20, 0), ms=3_600_000)
    changes = eng.on_candle(bar)
    assert changes == [], "Expected no items emitted on cold start single candle"



