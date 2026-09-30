"""Trend agent rules on hand-made candles, no-look-ahead, live == batch (no network)."""

import itertools
import random
import zlib
from typing import Any

import pytest

from deskmind.agents.trend.agent import SymbolTrendAgent, run_batch
from deskmind.agents.trend.checks import convergence_check, truncation_check
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.models import Bar, as_of

H = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
# Small windows so hand-made fixtures stay readable. Same rules as production.
CFG = TrendConfig(left_bars=2, right_bars=2, atr_period=3, min_swing_atr=1.0,
                  trendline_tolerance_atr=0.25, range_after_bars=1000)  # fmt: skip


def path(points: list[float], steps: int = 3, spread: float = 0.5, iv: int = H) -> list[Bar]:
    """Candles walking linearly between the given turning points (close = path value)."""
    values = [points[0]]
    for a, b in itertools.pairwise(points):
        values += [a + (b - a) * k / steps for k in range(1, steps + 1)]
    bars, prev = [], values[0]
    for i, v in enumerate(values):
        t = T0 + i * iv
        bars.append(Bar(t, t + iv, prev, max(prev, v) + spread, min(prev, v) - spread, v))
        prev = v
    return bars


def run(bars: list[Bar], cfg: TrendConfig = CFG) -> tuple[TrendEngine, dict[str, dict]]:
    eng = TrendEngine("TEST", "1h", cfg)
    items: dict[str, dict] = {}
    for b in bars:
        for it in eng.on_candle(b):
            items[it["id"]] = it
    return eng, items


def kinds(items: dict[str, dict], kind: str) -> list[dict]:
    return sorted((i for i in items.values() if i["kind"] == kind), key=lambda i: i["pivot_time"])


UP = [105, 100, 110, 104, 116, 108, 122, 115]  # lows 100 < 104 < 108, highs 110 < 116 < 122
DOWN = [95, 100, 90, 96, 84, 92, 78, 85]  # highs 100 > 96 > 92, lows 90 > 84 > 78


def test_hh_hl_sequence() -> None:
    _, items = run(path(UP))
    assert [(s["price"], s["label"]) for s in kinds(items, "swing_low")] == [
        (99.5, None), (103.5, "HL"), (107.5, "HL")]  # fmt: skip
    assert [(s["price"], s["label"]) for s in kinds(items, "swing_high")] == [
        (110.5, None), (116.5, "HH"), (122.5, "HH")]  # fmt: skip


def test_lh_ll_sequence_and_bos_down() -> None:
    eng, items = run(path(DOWN))
    assert [s["label"] for s in kinds(items, "swing_high")] == [None, "LH", "LH"]
    assert [s["label"] for s in kinds(items, "swing_low")] == [None, "LL", "LL"]
    bos = kinds(items, "bos")
    assert bos and all(b["direction"] == "down" for b in bos)
    assert eng.trend == "bearish" and eng.reason == "bos_down"


def test_bos_up_uses_close_and_is_confirmed_at_break_candle_close() -> None:
    bars = path(UP)
    eng, items = run(bars)
    first = kinds(items, "bos")[0]
    assert first["direction"] == "up" and first["type"] == "BOS"  # ranging -> first break is BOS
    brk_bar = next(b for b in bars if b.open_time == first["pivot_time"])
    assert brk_bar.close > first["level"] and first["confirmed_at"] == brk_bar.close_time
    prev_bar = next(b for b in bars if b.close_time == brk_bar.open_time)
    assert prev_bar.close <= first["level"]  # the break is the FIRST close beyond the level
    assert eng.trend == "bullish"


def test_choch_against_trend() -> None:
    # Uptrend (HH/HL), then a close below the last swing low (108) -> CHoCH down.
    eng, items = run(path([*UP, 100, 104]))
    choch = kinds(items, "choch")
    assert len(choch) == 1 and choch[0]["direction"] == "down"
    assert choch[0]["level"] == pytest.approx(107.5)
    assert eng.trend == "bearish" and eng.reason == "choch_down"


def test_wick_through_level_is_sweep_not_bos() -> None:
    bars = path([105, 100, 110, 104, 108])  # swing high 110.5 confirmed, price back at ~108
    last = bars[-1]
    wick = Bar(last.open_time + H, last.close_time + H, last.close, 111.5, 107.0, 109.0)
    closing = Bar(wick.open_time + H, wick.close_time + H, 109.0, 112.0, 108.5, 111.8)
    _, items = run([*bars, wick, closing])
    sweeps = kinds(items, "sweep")
    assert [(s["direction"], s["pivot_time"]) for s in sweeps] == [("up", wick.open_time)]
    assert sweeps[0]["extreme"] == 111.5 and sweeps[0]["close"] == 109.0
    [bos] = kinds(items, "bos")
    assert bos["pivot_time"] == closing.open_time  # the close break, not the wick


def test_trendline_needs_three_touches() -> None:
    # Lows 100, 104, 108 equally spaced -> collinear: valid support line with 3 touches.
    _, items = run(path(UP))
    lines = kinds(items, "trendline_support")
    assert len(lines) == 1 and len(lines[0]["touches"]) == 3
    assert lines[0]["broken_at"] is None
    # Two collinear lows + a third low far off the line -> no line.
    _, items2 = run(path([105, 100, 110, 104, 116, 112, 122, 115]))
    assert kinds(items2, "trendline_support") == []


def test_trendline_break_on_close_through() -> None:
    bars = path([*UP, 95], steps=3)  # after the 3rd touch, a sharp drop closes through the line
    _, items = run(bars)
    [line] = kinds(items, "trendline_support")
    assert line["broken_at"] is not None
    brk = next(b for b in bars if b.close_time == line["broken_at"])
    v = line["a_price"] + line["slope_per_ms"] * (brk.open_time - line["a_time"])
    assert brk.close < v - line["tol"]


def test_as_of_hides_future_changes() -> None:
    _, items = run(path([*UP, 95]))
    [line] = kinds(items, "trendline_support")
    before_break = line["broken_at"] - 1
    view = as_of(line, before_break)
    assert view is not None and view["broken_at"] is None
    assert as_of(line, line["confirmed_at"] - 1) is None


def random_walk(n: int, seed: int, iv: int = H) -> list[Bar]:
    rng = random.Random(seed)
    bars, p = [], 100.0
    for i in range(n):
        o = p
        c = max(1.0, o + rng.gauss(0, 1.2))
        h = max(o, c) + abs(rng.gauss(0, 0.6))
        lo = min(o, c) - abs(rng.gauss(0, 0.6))
        t = T0 + i * iv
        bars.append(Bar(t, t + iv, round(o, 2), round(h, 2), round(lo, 2), round(c, 2)))
        p = c
    return bars


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_look_ahead_on_random_walks(seed: int) -> None:
    r = truncation_check(random_walk(1500, seed), n_cuts=15, seed=seed)
    assert r.passed == r.cuts, r.failures
    assert r.items_compared > 100


def test_start_point_convergence() -> None:
    res = convergence_check(random_walk(2000, 9), offset=250)
    assert res["converged_after_bars"] is not None and res["converged_after_bars"] < 500


def aggregate(bars5: list[Bar], factor: int) -> list[Bar]:
    out = []
    for i in range(0, len(bars5) - factor + 1, factor):
        ch = bars5[i : i + factor]
        out.append(Bar(ch[0].open_time, ch[-1].close_time, ch[0].open, max(b.high for b in ch),
                       min(b.low for b in ch), ch[-1].close))  # fmt: skip
    return out


async def test_live_path_equals_batch_path() -> None:
    import fakeredis

    from deskmind.agents.trend.runner import LiveTrendRunner
    from deskmind.agents.trend.store import MemoryTrendStore
    from deskmind.config import Settings
    from deskmind.data.live import buffers as B
    from deskmind.data.live.buffers import Kline

    m5 = random_walk(600, 5, iv=300_000)
    by_tf = {"5m": m5, "15m": aggregate(m5, 3), "1h": aggregate(m5, 12)}
    tfs = list(by_tf)

    batch = run_batch(SymbolTrendAgent("BTCUSDT", tfs, CFG), by_tf)

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    settings = Settings(_env_file=None, postgres_password="x", data_symbols="BTCUSDT",
                        live_timeframes="5m,15m,1h")  # fmt: skip
    store = MemoryTrendStore()
    runner = LiveTrendRunner(redis, store, None, settings, CFG)
    runner.agents["BTCUSDT"] = SymbolTrendAgent("BTCUSDT", tfs, CFG)
    live_reports = []
    closes = sorted({b.close_time for bars in by_tf.values() for b in bars})
    for t in closes:
        closed = {tf: [b for b in bars if b.close_time == t] for tf, bars in by_tf.items()}
        closed = {tf: b for tf, b in closed.items() if b}
        for tf, bars in closed.items():
            k = [
                Kline(
                    "BTCUSDT",
                    tf,
                    b.open_time,
                    b.close_time - 1,
                    *(_d(x) for x in (b.open, b.high, b.low, b.close)),
                    _d(1),
                    _d(1),
                    1,
                    _d(0),
                    _d(0),
                )
                for b in bars
            ]
            await B.put_closed(redis, k)
        live_reports.append(
            await runner.on_bar_close(
                {"symbol": "BTCUSDT", "close_time": t, "closed_timeframes": list(closed)}
            )
        )
    assert live_reports == batch.reports
    live_items = {(tf, item_id): it for (_, tf, item_id), it in store.events.items()}
    assert live_items == batch.items


def _d(x: float):
    from decimal import Decimal

    return Decimal(str(x))


def _stored_symbols() -> list[str]:
    from deskmind.config import get_settings

    s = get_settings()
    return [x for x in s.data_symbols if (s.data_dir / "parquet" / x / "klines" / "5m").exists()]


@pytest.mark.skipif(not _stored_symbols(), reason="no stored Parquet history")
@pytest.mark.parametrize("tf", ["5m", "15m", "1h", "2h"])
def test_no_look_ahead_on_real_data_all_symbols(tf: str) -> None:
    """Mandatory check on real candles: every stored symbol x timeframe, random cut points."""
    import asyncio
    from datetime import UTC, datetime

    from deskmind.agents.candles import load_bars
    from deskmind.config import get_settings

    s = get_settings()
    now = int(datetime.now(UTC).timestamp() * 1000)
    for sym in _stored_symbols():
        bars = asyncio.run(load_bars(s.data_dir, None, sym, tf, end_ms=now, last_n=1500))
        # Reproducible cut points (crc32, not hash(): that changes per process).
        r = truncation_check(bars, n_cuts=15, seed=zlib.crc32(f"{sym}{tf}".encode()))
        assert r.passed == r.cuts, (sym, tf, r.failures)


def test_trendline_expires_after_max_idle_bars() -> None:
    from dataclasses import replace

    cfg = replace(CFG, trendline_max_idle_bars=12)
    # Valid 3-touch support, then price drifts far above it without touching again.
    bars = path([*UP, 130, 125, 140, 135, 150], steps=3)
    _, items = run(bars, cfg)
    line = min(kinds(items, "trendline_support"), key=lambda ln: ln["a_time"])  # the original
    assert line["a_price"] == pytest.approx(99.5)
    assert line["broken_at"] is None and line["expired_at"] is not None
    last_touch_bar = next(
        i for i, b in enumerate(bars) if b.open_time == line["touches"][-1]["pivot_time"]
    )
    expired_bar = next(i for i, b in enumerate(bars) if b.close_time == line["expired_at"])
    assert expired_bar - last_touch_bar == cfg.trendline_max_idle_bars + 1


def test_expired_line_is_not_recreated_with_same_id() -> None:
    """Regression (1.1.1): anchors reused after expiry changed an item's confirmed_at."""
    from dataclasses import replace

    cfg = replace(CFG, trendline_max_idle_bars=12)
    # 3-touch support, idle -> expires, then a new low lands on the old line again.
    bars = path([*UP, 130, 125, 140, 135, 150, 118], steps=3)
    eng, items = run(bars, cfg)
    lines = kinds(items, "trendline_support")
    assert len({ln["id"] for ln in lines}) == len(lines)
    first = min(lines, key=lambda ln: ln["a_time"])
    assert first["expired_at"] is not None
    assert first["id"] in eng.line_ids
    r = truncation_check(bars, n_cuts=len(bars) - 20, cfg=cfg)
    assert r.passed == r.cuts, r.failures


def test_consecutive_wicks_produce_single_sweep() -> None:
    """Consecutive candles wicking beyond the same swing without closing only produce 1 sweep."""
    # Establish a swing low at 100.
    bars = path([108, 100, 110], steps=3, spread=0.2)
    eng, items = run(bars)
    swing_lows = kinds(items, "swing_low")
    assert len(swing_lows) >= 1
    target_low = swing_lows[0]["price"]

    # Now add 3 consecutive candles whose low < target_low but close > target_low.
    t_last = bars[-1].close_time
    extra = [
        Bar(t_last + 0 * H, t_last + 1 * H, 102.0, 103.0, target_low - 0.5, 101.5),
        Bar(t_last + 1 * H, t_last + 2 * H, 101.5, 102.5, target_low - 0.8, 101.2),
        Bar(t_last + 2 * H, t_last + 3 * H, 101.2, 102.8, target_low - 0.3, 101.8),
    ]
    all_sweeps = []
    for b in extra:
        changes = eng.on_candle(b)
        for it in changes:
            if it["kind"] == "sweep":
                all_sweeps.append(it)

    # Exactly one sweep emitted, not 3
    assert len(all_sweeps) == 1
    assert all_sweeps[0]["level"] == target_low
    assert all_sweeps[0]["pivot_time"] == extra[0].open_time


# ---------------------------------------------------------------- point-in-time storage (1.2.0)


def _walk(n: int, seed: int, iv: int = H) -> list[Bar]:
    rng = random.Random(seed)
    bars, price = [], 100.0
    for i in range(n):
        o = price
        price = max(5.0, price + rng.gauss(0, 1.2) + (0.9 if (i // 70) % 2 else -0.9))
        hi = max(o, price) + abs(rng.gauss(0, 0.6))
        lo = min(o, price) - abs(rng.gauss(0, 0.6))
        t = T0 + i * iv
        bars.append(Bar(t, t + iv, o, hi, lo, price))
    return bars


@pytest.mark.parametrize("seed", [4, 17])
def test_state_reconstructed_from_transitions_equals_engine_state_at_random_t(seed: int) -> None:
    """Point-in-time trend state from items + transitions == the live engine state at T."""
    import copy
    import json

    from deskmind.agents.transitions import rows_by_tf
    from deskmind.agents.trend.history import (
        TrendTransitionRecorder,
        bar_context,
        reconstruct_state,
    )

    cfg = TrendConfig(left_bars=2, right_bars=2, atr_period=5, min_swing_atr=1.0,
                      trendline_tolerance_atr=0.4, range_after_bars=40)  # fmt: skip
    h1 = _walk(2400, seed)
    h2 = [
        Bar(a.open_time, b.close_time, a.open, max(a.high, b.high), min(a.low, b.low), b.close)
        for a, b in zip(h1[::2], h1[1::2], strict=False)
    ]
    bars = {"1h": h1, "2h": h2}
    by_close: dict[int, dict[str, list[Bar]]] = {}
    for tf, bs in bars.items():
        for b in bs:
            by_close.setdefault(b.close_time, {}).setdefault(tf, []).append(b)
    agent = SymbolTrendAgent("TEST", ["1h", "2h"], cfg)
    rec = TrendTransitionRecorder()
    store_from = h1[500].close_time
    bounds = sorted(by_close)
    rng = random.Random(seed)
    check = set(rng.sample([t for t in bounds if t > store_from], 60))
    check |= {next(t for t in bounds if t >= store_from), bounds[-1]}
    seen: dict[str, list[Bar]] = {"1h": [], "2h": []}
    live: dict[int, dict[str, Any]] = {}
    for t in bounds:
        out = agent.on_bar_close(t, by_close[t], with_report=False)
        for tf, bs in by_close[t].items():
            seen[tf].extend(bs)
        if t >= store_from:
            if rec.started:
                rec.boundary(t, agent, out.changes)
            else:
                rec.baseline(t, agent)
        if t in check:
            live[t] = {
                tf: (copy.deepcopy(e.state()), bar_context(seen[tf], cfg))
                for tf, e in agent.engines.items()
            }
    rows = rows_by_tf(json.loads(json.dumps(rec.rows)))  # stored as JSONB
    eng = agent.engines["1h"]
    assert eng.lines or any(r["kind"].startswith("trendline") for r in rows["1h"])
    assert any(r["kind"] in ("bos", "choch") for r in rows["1h"])
    ranging = {s["trend"] for p in live.values() for s, _ in p.values()}
    assert "ranging" in ranging and ranging - {"ranging"}  # timeouts + real trends covered
    n = 0
    for t, per_tf in live.items():
        for tf, (state, (as_of_ctx, atr_ctx)) in per_tf.items():
            got = reconstruct_state(rows[tf], t, cfg, as_of_ctx, atr_ctx)
            assert json.loads(json.dumps(got)) == json.loads(json.dumps(state)), (t, tf)
            n += 1
    assert n == 2 * len(check)


def test_bar_context_matches_engine_as_of_and_atr() -> None:
    from deskmind.agents.trend.history import bar_context

    bars = _walk(300, seed=2)
    eng = TrendEngine("TEST", "1h", CFG)
    for i, b in enumerate(bars):
        eng.on_candle(b)
        if i >= CFG.atr_period:
            assert bar_context(bars[: i + 1], CFG) == (eng.as_of, eng.atr)


def test_backfill_run_batch_without_reports_keeps_items_identical() -> None:
    bars = {"1h": _walk(1500, seed=9)}
    a = run_batch(SymbolTrendAgent("TEST", ["1h"], CFG), bars, store_from=T0 + 400 * H)
    b = run_batch(SymbolTrendAgent("TEST", ["1h"], CFG), bars, store_from=T0 + 400 * H,
                  reports=False)  # fmt: skip
    assert a.items == b.items and b.reports == [] and len(a.reports) == 1500


def test_trend_cold_start_warmup() -> None:
    """Engine refuses to emit items on a single synthetic bar without warmup."""
    from deskmind.agents.trend.models import Bar
    eng = TrendEngine("BTC", "1h")
    bar = Bar(open_time=1000, close_time=3601000, open=100.0, high=101.0, low=99.0, close=100.5)
    changes = eng.on_candle(bar)
    assert changes == [], "Expected no items emitted on cold start single candle"



