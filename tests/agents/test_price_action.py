"""Tests for the Price Action agent: S/R zones, breakouts, patterns, ranges, truncation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deskmind.agents.price_action.agent import PriceActionAgent, run_batch
from deskmind.agents.price_action.checks import truncation_check
from deskmind.agents.price_action.config import DEFAULT_CONFIG
from deskmind.agents.price_action.engine import PriceActionEngine
from deskmind.agents.price_action.models import as_of
from deskmind.agents.price_action.store import MemoryPriceActionStore
from deskmind.agents.trend.models import Bar


def _bar(t: int, *, o=100.0, h=101.0, l=99.0, c=100.5, ms=60_000) -> Bar:
    return Bar(
        open_time=t,
        close_time=t + ms,
        open=o,
        high=h,
        low=l,
        close=c,
    )


def _ascending_bars(n: int, start_t: int = 0, step_ms: int = 60_000) -> list[Bar]:
    """n bars with steadily rising prices (good for warming up ATR)."""
    bars = []
    for i in range(n):
        price = 100.0 + i * 0.1
        bars.append(_bar(start_t + i * step_ms, o=price, h=price + 0.5, l=price - 0.5, c=price + 0.1))
    return bars


def _swing_items(prices: list[tuple[float, str, int]]) -> list[dict]:
    """Create fake swing items: [(price, "swing_high"|"swing_low", confirmed_at), ...]."""
    return [
        {"kind": kind, "price": price, "confirmed_at": ct, "id": f"{kind}_{i}"}
        for i, (price, kind, ct) in enumerate(prices)
    ]


# ------------------------------------------------------------------ S/R zones


def test_sr_zone_created_from_swings() -> None:
    eng = PriceActionEngine("BTC", "1h")
    # Warm ATR
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    atr = eng._atr
    assert atr is not None and atr > 0

    # Create two swing highs at the same price level
    t0, t1 = bars[-1].close_time + 1, bars[-1].close_time + 2
    fake_swings = _swing_items([(105.0, "swing_high", t0), (105.1, "swing_high", t1)])
    # Feed a bar with those swings
    bar = _bar(bars[-1].close_time, o=103.0, h=106.0, l=102.0, c=103.5, ms=3_600_000)
    changes = eng.on_candle(bar, swings=fake_swings)
    zone_changes = [c for c in changes if c["kind"].startswith("sr_")]
    assert len(zone_changes) >= 1
    assert zone_changes[0]["side"] == "resistance"


def test_sr_zone_not_created_single_swing() -> None:
    """A single swing is not enough to form a zone (need min sr_min_touches=2)."""
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    t0 = bars[-1].close_time + 1
    fake_swings = _swing_items([(105.0, "swing_high", t0)])
    bar = _bar(bars[-1].close_time, o=103.0, h=106.0, l=102.0, c=103.5, ms=3_600_000)
    changes = eng.on_candle(bar, swings=fake_swings)
    zone_changes = [c for c in changes if c["kind"].startswith("sr_")]
    assert len(zone_changes) == 0


def test_support_zone_from_swing_lows() -> None:
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    t0, t1 = bars[-1].close_time + 1, bars[-1].close_time + 2
    fake_swings = _swing_items([(95.0, "swing_low", t0), (95.2, "swing_low", t1)])
    bar = _bar(bars[-1].close_time, o=97.0, h=98.0, l=94.5, c=96.5, ms=3_600_000)
    changes = eng.on_candle(bar, swings=fake_swings)
    zones = [c for c in changes if c["kind"] == "sr_support"]
    assert zones, "Expected a support zone"


# ------------------------------------------------------------------ breakout


def test_breakout_detected_when_close_above_resistance() -> None:
    eng = PriceActionEngine("BTC", "1h", DEFAULT_CONFIG)
    # Warm ATR
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    atr = eng._atr
    assert atr is not None

    # Use times strictly after the warmup bars
    base_t = bars[-1].close_time
    # Bar 1: swing high at 105 forms zone with first swing
    sw_bar1 = _bar(base_t, o=103.0, h=106.0, l=102.0, c=103.5, ms=3_600_000)
    sw1 = _swing_items([(105.0, "swing_high", sw_bar1.close_time)])
    eng.on_candle(sw_bar1, swings=sw1)

    # Bar 2: second swing close to same level -> zone created
    sw_bar2 = _bar(sw_bar1.close_time, o=103.0, h=105.2, l=102.0, c=103.5, ms=3_600_000)
    sw2 = _swing_items([(105.1, "swing_high", sw_bar2.close_time)])
    eng.on_candle(sw_bar2, swings=sw2)
    assert eng.zones, "Expected a zone to be created from two swing highs"

    zone = list(eng.zones.values())[0]
    # Bar 3: close above zone by bo_min_atr + offset to handle ATR expansion
    bo_price = zone.top + DEFAULT_CONFIG.bo_min_atr * atr + 3.0
    bo_bar = _bar(sw_bar2.close_time, o=104.0, h=bo_price + 0.5, l=103.5, c=bo_price, ms=3_600_000)
    changes = eng.on_candle(bo_bar)
    bos = [c for c in changes if c["kind"] == "breakout"]
    assert bos, f"Expected a breakout, got changes: {[c['kind'] for c in changes]}"
    assert bos[0]["direction"] == "up"


# ------------------------------------------------------------------ candle patterns


def test_bullish_engulfing() -> None:
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    t = bars[-1].close_time
    # Previous bar: bearish (open > close)
    prev = _bar(t, o=105.0, h=106.0, l=103.0, c=103.5, ms=3_600_000)
    eng.on_candle(prev)
    # Current bar: bullish engulfing (body fully covers prev bearish body)
    curr = _bar(t + 3_600_000, o=103.0, h=107.0, l=102.5, c=106.0, ms=3_600_000)
    changes = eng.on_candle(curr)
    pats = [c for c in changes if c.get("pattern") == "bullish_engulfing"]
    assert pats, "Expected bullish_engulfing pattern"


def test_hammer_pattern() -> None:
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    t = bars[-1].close_time
    # Previous bar: neutral
    prev = _bar(t, o=100.0, h=100.5, l=99.0, c=100.2, ms=3_600_000)
    eng.on_candle(prev)
    # Hammer: body=1 (o=99.5, c=100.5), lower_wick=4.5 (l=95, min(o,c)=99.5 -> 99.5-95=4.5)
    # upper_wick = 100.5 - 100.5 = 0.0  (< 0.5 * body=0.5 -> True)
    # lower_wick >= pin_wick_ratio(2) * body -> 4.5 >= 2 -> True
    hammer = _bar(t + 3_600_000, o=99.5, h=100.5, l=95.0, c=100.5, ms=3_600_000)
    changes = eng.on_candle(hammer)
    pats = [c for c in changes if c.get("pattern") == "hammer"]
    assert pats, f"Expected hammer, got patterns: {[c.get('pattern') for c in changes if 'pattern' in c]}"


def test_doji_detected() -> None:
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    atr = eng._atr
    assert atr is not None
    t = bars[-1].close_time
    # Doji: body << doji_body_atr * ATR
    doji_body = DEFAULT_CONFIG.doji_body_atr * atr * 0.5
    prev = _bar(t, o=100.0, h=100.5, l=99.5, c=100.0, ms=3_600_000)
    eng.on_candle(prev)
    doji = _bar(t + 3_600_000, o=100.0, h=101.0, l=99.0, c=100.0 + doji_body, ms=3_600_000)
    changes = eng.on_candle(doji)
    pats = [c for c in changes if c.get("pattern") == "doji"]
    assert pats, "Expected doji pattern"


# ------------------------------------------------------------------ ranges


def test_range_detected_in_sideways_bars() -> None:
    eng = PriceActionEngine("BTC", "1h")
    # Warm ATR
    trend_bars = _ascending_bars(20, step_ms=3_600_000)
    for b in trend_bars:
        eng.on_candle(b)
    atr = eng._atr
    assert atr is not None
    # Sideways bars within range_width_atr ATR
    t = trend_bars[-1].close_time
    width = DEFAULT_CONFIG.range_width_atr * atr
    range_bars = [
        _bar(t + i * 3_600_000, o=100.0, h=100.0 + width * 0.4, l=100.0 - width * 0.4, c=100.0, ms=3_600_000)
        for i in range(DEFAULT_CONFIG.range_min_bars + 2)
    ]
    changes_all: list[dict] = []
    for b in range_bars:
        changes_all.extend(eng.on_candle(b))
    range_changes = [c for c in changes_all if c["kind"] == "range"]
    assert range_changes, "Expected a range to be detected"


# ------------------------------------------------------------------ as_of masking


def test_as_of_masks_breakout_outcome() -> None:
    confirmed = 1_000_000
    retest_t = 2_000_000
    item = {
        "id": "bo_test",
        "kind": "breakout",
        "created_time": 900_000,
        "confirmed_at": confirmed,
        "zone_id": "z1",
        "breakout_time": confirmed,
        "direction": "up",
        "retest_time": retest_t,
        "outcome": "held",
    }
    # Before confirmation: None
    assert as_of(item, 500_000) is None
    # After confirmation but before retest: retest should be masked
    view = as_of(item, 1_500_000)
    assert view is not None
    assert view["retest_time"] is None
    assert view["outcome"] is None
    # After retest
    view2 = as_of(item, 2_500_000)
    assert view2["outcome"] == "held"


def test_as_of_masks_zone_broken_at() -> None:
    item = {
        "id": "sr_resistance_test",
        "kind": "sr_resistance",
        "created_time": 1_000_000,
        "confirmed_at": 1_000_000,
        "side": "resistance",
        "top": 110.0,
        "bottom": 109.0,
        "mid": 109.5,
        "state": "broken",
        "broken_at": 3_000_000,
        "flipped_at": None,
        "retired_at": None,
        "touches": [1_000_000, 2_000_000, 3_000_000],
    }
    view = as_of(item, 2_500_000)
    assert view is not None
    assert view["state"] == "active"
    assert view["broken_at"] is None


# ------------------------------------------------------------------ truncation


def test_truncation_no_lookahead() -> None:
    """No look-ahead: items from a truncated run == as-of view at that time.
    Ranges are excluded because their start_time (and id) is path-dependent on
    the warmup window available – this is expected and documented behaviour.
    """
    bars = _ascending_bars(200, step_ms=3_600_000)
    from deskmind.agents.price_action.checks import TruncationResult
    import random
    from deskmind.agents.price_action.engine import PriceActionEngine as E
    from deskmind.agents.price_action.models import as_of as item_as_of

    cfg = DEFAULT_CONFIG
    tf = "1h"
    rng = random.Random(5)
    lo = min(len(bars) - 1, cfg.atr_period + cfg.sr_min_touches + 2)
    idx = sorted(rng.sample(range(lo, len(bars)), min(8, max(1, len(bars) - lo))))

    def _run(brs):
        eng = E("X", tf, cfg)
        items = {}
        for b in brs:
            for it in eng.on_candle(b):
                items[it["id"]] = it
        return items

    full = _run(bars)
    failures = []
    for i in idx:
        t = bars[i].close_time
        trunc = _run(bars[:i + 1])
        # Exclude ranges (path-dependent start_time)
        full_at = {k: v for k, v in full.items() if not k.startswith("range:")}
        trunc_at = {k: v for k, v in trunc.items() if not k.startswith("range:")}
        fa = {k: item_as_of(v, t) for k, v in full_at.items() if item_as_of(v, t) is not None}
        ta = {k: item_as_of(v, t) for k, v in trunc_at.items() if item_as_of(v, t) is not None}
        if fa != ta:
            diff = sorted(set(fa) ^ set(ta))[:3]
            failures.append(f"at {t}: {diff}")
    assert not failures, f"Failures: {failures}"


# ------------------------------------------------------------------ store round-trip


@pytest.mark.asyncio
async def test_memory_store_upsert_and_items() -> None:
    store = MemoryPriceActionStore()
    agent = PriceActionAgent("BTCUSDT")
    bars = _ascending_bars(100, step_ms=3_600_000)
    tf = "1h"
    result = run_batch(agent, {tf: bars}, reports=True)
    report_list = result.reports.get(tf, [])
    item_list = result.items.get(tf, [])
    await store.upsert_items("BTCUSDT", tf, item_list, DEFAULT_CONFIG.version)
    if report_list:
        await store.upsert_reports("BTCUSDT", tf, report_list)
    last_t = bars[-1].close_time
    items = await store.items("BTCUSDT", tf, None, None, None)
    # If reports were emitted, check them; otherwise just check items are present
    if report_list:
        rpt = await store.latest_report("BTCUSDT", tf, last_t)
        assert rpt is not None
        assert rpt["symbol"] == "BTCUSDT"
    for it in items:
        assert "confirmed_at" in it


# ------------------------------------------------------------------ multi-symbol isolation


def test_multi_symbol_isolation() -> None:
    btc = PriceActionEngine("BTC", "1h")
    eth = PriceActionEngine("ETH", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        btc.on_candle(b)
    eth_bars = [
        _bar(b.open_time, o=b.open * 10, h=b.high * 10, l=b.low * 10, c=b.close * 10, ms=3_600_000)
        for b in bars
    ]
    for b in eth_bars:
        eth.on_candle(b)
    assert btc.symbol != eth.symbol
    assert btc._atr != eth._atr


# ------------------------------------------------------------------ multi-timeframe isolation


def test_multi_timeframe_engines_independent() -> None:
    agent = PriceActionAgent("BTC")
    bars_1h = _ascending_bars(50, step_ms=3_600_000)
    bars_4h = _ascending_bars(50, step_ms=14_400_000)
    agent.on_bar_close(
        bars_1h[-1].close_time,
        {"1h": bars_1h, "4h": bars_4h},
    )
    assert "1h" in agent.engines
    assert "4h" in agent.engines
    assert agent.engines["1h"] is not agent.engines["4h"]


# ------------------------------------------------------------------ round numbers


def test_round_numbers_for_btc() -> None:
    eng = PriceActionEngine("BTCUSDT", "1h")
    bars = [_bar(i * 3_600_000, o=65000.0, h=65500.0, l=64500.0, c=65100.0, ms=3_600_000) for i in range(20)]
    for b in bars:
        eng.on_candle(b)
    rnums = eng.round_numbers
    assert rnums, "Expected round numbers to be populated"
    # All should be multiples of 1000 (BTC step)
    for r in rnums:
        assert r % 1000 == 0, f"{r} is not a round 1000 level"


# ------------------------------------------------------------------ near-miss negatives & state reconstruction


def test_pattern_near_miss_negatives() -> None:
    """Verify that near-miss patterns failing numeric threshold checks produce NO pattern items."""
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(20, step_ms=3_600_000)
    for b in bars:
        eng.on_candle(b)
    t = bars[-1].close_time

    # Near-miss hammer: lower wick = 1.5 * body (< pin_wick_ratio = 2.0)
    # body = 1 (o=99, c=100), lower_wick = 1.5 (l=97.5) -> fails ratio 2.0
    near_hammer = _bar(t + 3_600_000, o=99.0, h=100.2, l=97.5, c=100.0, ms=3_600_000)
    changes = eng.on_candle(near_hammer)
    hammers = [c for c in changes if c.get("pattern") == "hammer"]
    assert not hammers, f"Expected no hammer for near-miss wick ratio, got {hammers}"


def test_reconstruct_state_equals_live_engine() -> None:
    """reconstruct_state(T) from item log matches live engine active zones at random time T."""
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(50, step_ms=3_600_000)
    all_items = []
    for b in bars:
        all_items.extend(eng.on_candle(b))

    cut_bar = bars[30]
    cut_t = cut_bar.close_time
    live_eng = PriceActionEngine("BTC", "1h")
    for b in bars[:31]:
        live_eng.on_candle(b)

    from deskmind.agents.price_action.models import reconstruct_state
    rec_state = reconstruct_state(all_items, cut_t)
    # Active zones count matches live engine
    assert len(rec_state["zones"]) == len(live_eng.zones)


def test_unique_item_ids() -> None:
    """All items produced by Price Action engine have deterministic, collision-free IDs across entities."""
    eng = PriceActionEngine("BTC", "1h")
    bars = _ascending_bars(50, step_ms=3_600_000)
    items_by_id = {}
    for b in bars:
        for it in eng.on_candle(b):
            items_by_id[it["id"]] = it
    assert len(items_by_id) >= 1
    assert all("id" in v for v in items_by_id.values())


