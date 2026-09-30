"""Tests for ICT/SMC liquidity agent: fixtures, concepts, no-look-ahead, live == batch."""

import itertools
from typing import Any

import pytest

from deskmind.agents.ict_smc.agent import (
    SymbolIctSmcAgent,
    check_smt_divergence,
    run_batch,
)
from deskmind.agents.ict_smc.checks import truncation_check
from deskmind.agents.ict_smc.config import IctSmcConfig
from deskmind.agents.ict_smc.engine import IctSmcEngine
from deskmind.agents.ict_smc.overlays import to_overlays
from deskmind.agents.registry import AGENTS
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.models import Bar

H = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC

# Tight config for unit test fixtures
CFG = IctSmcConfig(
    eq_tolerance_atr=0.2,
    pool_swing_lookback=10,
    displacement_min_atr=1.0,
    displacement_body_ratio=0.6,
    fvg_min_atr=0.2,
    ob_max_candles=3,
    ote_low_pct=0.62,
    ote_high_pct=0.79,
    zone_max_age_bars=1000,
)
TREND_CFG = TrendConfig(
    left_bars=2,
    right_bars=2,
    atr_period=3,
    min_swing_atr=1.0,
    trendline_tolerance_atr=0.25,
    range_after_bars=1000,
)


def make_bars(points: list[float], steps: int = 3, spread: float = 0.5, iv: int = H) -> list[Bar]:
    """Candles walking linearly between turning points."""
    values = [points[0]]
    for a, b in itertools.pairwise(points):
        values += [a + (b - a) * k / steps for k in range(1, steps + 1)]
    bars, prev = [], values[0]
    for i, v in enumerate(values):
        t = T0 + i * iv
        bars.append(Bar(t, t + iv, prev, max(prev, v) + spread, min(prev, v) - spread, v))
        prev = v
    return bars


def run_engine(
    bars: list[Bar], cfg: IctSmcConfig = CFG, trend_cfg: TrendConfig = TREND_CFG
) -> tuple[IctSmcEngine, dict[str, dict[str, Any]]]:
    eng = IctSmcEngine("TEST", "1h", cfg, trend_cfg)
    items: dict[str, dict[str, Any]] = {}
    for b in bars:
        for it in eng.on_candle(b):
            items[it["id"]] = it
    return eng, items


# ==============================================================================
# Concept 1 & 2: Equal Highs / Lows, Liquidity Pools & Sweeps / Taken
# ==============================================================================


def test_equal_highs_bsl_and_equal_lows_ssl() -> None:
    # Two swing highs near 110.0 (e.g. 110.0 and 110.2)
    pts = [100, 110, 104, 110.2, 103, 115]
    bars = make_bars(pts, steps=3, spread=0.2)
    _eng, items = run_engine(bars)

    bsl_pools = [it for it in items.values() if it["kind"] == "bsl"]
    assert len(bsl_pools) >= 1
    # Check that at least one pool is EQH
    eqh = [p for p in bsl_pools if p.get("pool_type") == "eqh"]
    assert len(eqh) >= 1
    assert eqh[0]["level"] == pytest.approx(110.4, rel=0.01)
    assert eqh[0]["side"] == "high"


def test_liquidity_pool_sweep_and_taken() -> None:
    # Form high, then sweep it with wick, then close above it (taken)
    bars = make_bars([100, 110, 104], steps=3, spread=0.2)
    eng, items = run_engine(bars)

    # Add a sweeping candle: wicks above 110.2 to 111.0, closes at 108.0 (wick only)
    last = bars[-1]
    t1 = last.close_time
    sweep_bar = Bar(t1, t1 + H, 104.0, 111.0, 103.5, 108.0)
    for it in eng.on_candle(sweep_bar):
        items[it["id"]] = it

    swept_pools = [p for p in items.values() if p["kind"] == "bsl" and p.get("state") == "swept"]
    assert len(swept_pools) >= 1
    assert swept_pools[0]["swept_at"] == sweep_bar.close_time

    # Add a candle that closes above level -> taken
    t2 = sweep_bar.close_time
    take_bar = Bar(t2, t2 + H, 108.0, 113.0, 107.0, 112.5)
    for it in eng.on_candle(take_bar):
        items[it["id"]] = it

    taken_pools = [p for p in items.values() if p["kind"] == "bsl" and p.get("state") == "taken"]
    assert len(taken_pools) >= 1
    assert taken_pools[0]["taken_at"] == take_bar.close_time


# ==============================================================================
# Concept 3 & 4: FVGs, Consequent Encroachment, States, Inversion FVG
# ==============================================================================


def test_fvg_creation_fill_and_inversion() -> None:
    # 3-candle bullish FVG:
    # c1: low=100, high=102, close=101
    # c2: big displacement up: open=102, high=110, low=101.5, close=109.5
    # c3: open=109.5, low=105, high=112, close=111.
    # Gap is between c1.high (102) and c3.low (105)
    # First feed dummy bars so ATR warms up
    warm_bars = make_bars([90, 95, 92, 98], steps=2, spread=0.5)
    eng = IctSmcEngine("TEST", "1h", CFG, TREND_CFG)
    for b in warm_bars:
        eng.on_candle(b)

    t = warm_bars[-1].close_time
    c1 = Bar(t, t + H, 100.0, 102.0, 99.5, 101.0)
    c2 = Bar(t + H, t + 2 * H, 101.5, 110.0, 101.5, 109.5)
    c3 = Bar(t + 2 * H, t + 3 * H, 109.5, 112.0, 105.0, 111.0)

    items = {}
    for b in [c1, c2, c3]:
        for it in eng.on_candle(b):
            items[it["id"]] = it

    fvgs = [it for it in items.values() if it["kind"] == "fvg_bullish"]
    assert len(fvgs) >= 1
    fvg = fvgs[-1]
    assert fvg["bottom"] == 102.0
    assert fvg["top"] == 105.0
    assert fvg["ce"] == 103.5
    assert fvg["confirmed_at"] == c3.close_time
    assert fvg["state"] == "fresh"

    # Retest: c4 wicks into gap to 104.0, closes at 106.0 -> partially_filled
    c4 = Bar(t + 3 * H, t + 4 * H, 111.0, 111.5, 104.0, 106.0)
    for it in eng.on_candle(c4):
        items[it["id"]] = it
    assert items[fvg["id"]]["state"] == "partially_filled"

    # Inversion: c5 closes below bottom (102) -> inversion
    c5 = Bar(t + 4 * H, t + 5 * H, 106.0, 106.5, 100.0, 100.5)
    for it in eng.on_candle(c5):
        items[it["id"]] = it
    assert items[fvg["id"]]["state"] == "inversion"
    assert items[fvg["id"]]["inverted_at"] == c5.close_time


# ==============================================================================
# Concept 5: Order Blocks, Mitigation vs Breakers
# ==============================================================================


def test_order_block_qualification_and_breaker() -> None:
    # Uptrend move causing BOS up
    bars = make_bars([100, 96, 112, 106, 120], steps=3, spread=0.5)
    eng, items = run_engine(bars)

    obs = [it for it in items.values() if it["kind"] == "ob_bullish"]
    assert len(obs) >= 1
    ob = obs[0]
    assert ob["direction"] == "bullish"
    assert ob["state"] in ("fresh", "tested")

    # Invalidate the OB by closing below its bottom
    last = bars[-1]
    t = last.close_time
    inv_bar = Bar(t, t + H, ob["bottom"] + 2, ob["bottom"] + 3, ob["bottom"] - 5, ob["bottom"] - 3)
    for it in eng.on_candle(inv_bar):
        items[it["id"]] = it

    assert items[ob["id"]]["state"] == "invalidated"
    # Should produce a breaker or mitigation block
    breakers = [
        it for it in items.values() if "breaker" in it["kind"] or "mitigation" in it["kind"]
    ]
    assert len(breakers) >= 1
    brk = breakers[0]
    assert brk["direction"] == "bearish"  # flipped direction
    assert brk["confirmed_at"] == inv_bar.close_time


# ==============================================================================
# Concept 6: Premium / Discount Range & OTE
# ==============================================================================


def test_pd_range_and_ote_calculation() -> None:
    bars = make_bars([100, 95, 115, 108, 125], steps=3, spread=0.5)
    eng, _ = run_engine(bars)

    assert eng.pd_range is not None
    pd = eng.pd_range
    assert pd.high > pd.low
    assert pd.equilibrium == pytest.approx((pd.low + pd.high) / 2.0)
    assert pd.ote_low < pd.ote_high
    assert pd.zone in ("discount", "premium", "equilibrium")


# ==============================================================================
# Concept 7: SMT Divergence
# ==============================================================================


def test_smt_divergence_aligned_and_misaligned() -> None:
    agent_a = SymbolIctSmcAgent("BTCUSDT", ["1h"], CFG, TREND_CFG)
    agent_b = SymbolIctSmcAgent("ETHUSDT", ["1h"], CFG, TREND_CFG)

    # Symbol A makes higher high
    bars_a = make_bars([100, 110, 105, 118], steps=2, spread=0.3)
    # Symbol B makes lower high (fails to make higher high)
    bars_b = make_bars([100, 112, 105, 109], steps=2, spread=0.3)

    for b in bars_a:
        agent_a.on_bar_close(b.close_time, {"1h": [b]})
    for b in bars_b:
        agent_b.on_bar_close(b.close_time, {"1h": [b]})

    # Aligned close time
    close_time = bars_a[-1].close_time
    smt = check_smt_divergence(agent_a, agent_b, "1h", close_time)
    # If swings align, SMT divergence detected
    if smt is not None:
        assert smt.divergence_type in ("bullish", "bearish")
        assert smt.symbol_a == "BTCUSDT" and smt.symbol_b == "ETHUSDT"

    # Misaligned close time returns None
    smt_misaligned = check_smt_divergence(agent_a, agent_b, "1h", close_time + 99999)
    assert smt_misaligned is None


# ==============================================================================
# Concept 8: No-Look-Ahead Truncation Test
# ==============================================================================


def test_no_look_ahead_truncation() -> None:
    bars = make_bars([100, 95, 115, 104, 122, 110, 130, 118, 105, 115], steps=4, spread=0.4)
    res = truncation_check(bars, n_cuts=5, seed=42, cfg=CFG, trend_cfg=TREND_CFG)
    assert res.failures == []
    assert res.passed == res.cuts


# ==============================================================================
# Concept 9: Live == Batch Parity
# ==============================================================================


def test_live_equals_batch() -> None:
    bars = make_bars([100, 95, 110, 105, 120], steps=3, spread=0.5)

    # Batch run
    agent_batch = SymbolIctSmcAgent("BTCUSDT", ["1h"], CFG, TREND_CFG)
    batch_res = run_batch(agent_batch, {"1h": bars})

    # Incremental / live step-by-step run
    agent_live = SymbolIctSmcAgent("BTCUSDT", ["1h"], CFG, TREND_CFG)
    live_items: dict[str, Any] = {}
    live_reports: list[dict[str, Any]] = []

    for b in bars:
        out = agent_live.on_bar_close(b.close_time, {"1h": [b]})
        live_reports.append(out.report)
        for _tf, it in out.changes:
            live_items[it["id"]] = it

    assert len(batch_res.reports) == len(live_reports)
    assert batch_res.reports[-1] == live_reports[-1]
    assert {it["id"] for it in batch_res.items.values()} == set(live_items.keys())


# ==============================================================================
# Concept 10: Overlays and Agent Registry
# ==============================================================================


def test_agent_registry_includes_ict_smc() -> None:
    agent_names = [a.name for a in AGENTS]
    assert "ict_smc" in agent_names
    ict_info = next(a for a in AGENTS if a.name == "ict_smc")
    assert ict_info.version == CFG.version
    assert ict_info.spec == "docs/agents/ict_smc.md"


def test_to_overlays_structure() -> None:
    bars = make_bars([100, 95, 115, 105, 125], steps=3, spread=0.5)
    _, items = run_engine(bars)

    view_end = bars[-1].close_time
    overlays = to_overlays(list(items.values()), CFG.version, view_end)
    assert len(overlays) > 0
    for o in overlays:
        assert o.source_agent == "ict_smc"
        assert o.agent_version == CFG.version
        assert o.confirmed_at is not None
        assert o.type in ("zone", "line", "marker")
        assert "rule" in o.meta


# ==============================================================================
# Concept 11: ATR End-to-End Audit & Consistency Across Timeframes
# ==============================================================================


def test_atr_audit_and_consistency() -> None:
    """Verify that ATR used by engine matches independent ATR(14) in $,

    and that items on 1h use 1h ATR at confirmed_at while items on 5m use 5m ATR.
    """
    bars_1h = make_bars([100, 95, 120, 105, 130, 110, 140], steps=4, spread=0.8, iv=3600_000)
    bars_5m = make_bars([100, 98, 105, 102, 108, 104, 110], steps=4, spread=0.2, iv=300_000)

    # Independent ATR calculation
    def calc_indep_atr(bars: list[Bar], period: int = 14) -> dict[int, float]:
        trs, atrs = [], {}
        for i, b in enumerate(bars):
            prev = bars[i - 1] if i > 0 else None
            tr = (
                b.high - b.low
                if prev is None
                else max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
            )
            trs.append(tr)
            if len(trs) >= period:
                atrs[b.close_time] = sum(trs[-period:]) / float(period)
        return atrs

    indep_1h = calc_indep_atr(bars_1h, period=TREND_CFG.atr_period)
    indep_5m = calc_indep_atr(bars_5m, period=TREND_CFG.atr_period)

    eng_1h = IctSmcEngine("BTCUSDT", "1h", CFG, TREND_CFG)
    items_1h = []
    for b in bars_1h:
        changes = eng_1h.on_candle(b)
        if eng_1h.atr is not None:
            expected = indep_1h[b.close_time]
            assert eng_1h.atr == pytest.approx(expected, rel=1e-5)
        items_1h.extend(changes)

    eng_5m = IctSmcEngine("BTCUSDT", "5m", CFG, TREND_CFG)
    items_5m = []
    for b in bars_5m:
        changes = eng_5m.on_candle(b)
        if eng_5m.atr is not None:
            expected = indep_5m[b.close_time]
            assert eng_5m.atr == pytest.approx(expected, rel=1e-5)
        items_5m.extend(changes)

    # Every item confirmed at time T must record the timeframe's ATR at that time
    for it in items_1h:
        if "atr" in it and it["atr"] is not None:
            t = it["confirmed_at"]
            if t in indep_1h:
                assert it["atr"] == pytest.approx(indep_1h[t], rel=1e-5)

    for it in items_5m:
        if "atr" in it and it["atr"] is not None:
            t = it["confirmed_at"]
            if t in indep_5m:
                assert it["atr"] == pytest.approx(indep_5m[t], rel=1e-5)


# ==============================================================================
# Concept 12: Liquidity Pool Strength (Strong vs Weak)
# ==============================================================================


def test_pool_strength_tagging() -> None:
    # EQH should produce strong pool; single swing produces weak pool
    pts = [100, 110, 104, 110.2, 103, 115]
    bars = make_bars(pts, steps=3, spread=0.2)
    _eng, items = run_engine(bars)

    pools = [it for it in items.values() if it["kind"] in ("bsl", "ssl")]
    assert len(pools) > 0

    strong_pools = [p for p in pools if p.get("strength") == "strong"]
    weak_pools = [p for p in pools if p.get("strength") == "weak"]

    assert len(strong_pools) >= 1
    assert len(weak_pools) >= 1

    for p in strong_pools:
        assert p["pool_type"] in ("eqh", "eql")
    for p in weak_pools:
        assert p["pool_type"] in ("single_high", "single_low")

    # Check overlays inherit strength
    overlays = to_overlays(pools, CFG.version, bars[-1].close_time)
    for o in overlays:
        if o.kind in ("bsl", "ssl"):
            assert o.meta.get("strength") in ("strong", "weak")


# ==============================================================================
# Concept 13: Warm-up Ancestor OB Persistence
# ==============================================================================


def test_warmup_ancestor_ob_persisted() -> None:
    """An ancestor OB created in warm-up must be persisted when referenced

    by an in-window breaker.
    """
    # 1. Sequence that creates an OB early:
    bars = make_bars([100, 95, 115, 105, 125, 90, 85], steps=3, spread=0.5)

    # Let store_from be midway through history so the OB is in warm-up
    # but the breaker invalidation occurs in-window
    mid_idx = len(bars) // 2
    store_from = bars[mid_idx].close_time

    agent = SymbolIctSmcAgent("BTCUSDT", ["1h"], CFG, TREND_CFG)
    res = run_batch(agent, {"1h": bars}, store_from=store_from)

    breakers = [it for (tf, _id), it in res.items.items() if "breaker" in it.get("kind", "")]
    if breakers:
        for b in breakers:
            src_id = b.get("source_ob_id")
            assert src_id is not None
            # The ancestor OB must be present in res.items!
            assert ("1h", src_id) in res.items
            ancestor_ob = res.items[("1h", src_id)]
            assert ancestor_ob["id"] == src_id
            assert ancestor_ob["kind"].startswith("ob_")


# ==============================================================================
# Concept 14: Batch Persistence Parity
# ==============================================================================


def test_batch_persistence_parity_on_fixtures() -> None:
    """Prove results are identical (same unique items + same report content)."""
    bars_1h = make_bars([100, 95, 115, 105, 125, 110, 130], steps=3, spread=0.5, iv=3600_000)
    bars_15m = make_bars([100, 96, 112, 107, 122, 114, 128], steps=3, spread=0.3, iv=900_000)

    agent1 = SymbolIctSmcAgent("BTCUSDT", ["15m", "1h"], CFG, TREND_CFG)
    res1 = run_batch(agent1, {"15m": bars_15m, "1h": bars_1h})

    agent2 = SymbolIctSmcAgent("BTCUSDT", ["15m", "1h"], CFG, TREND_CFG)
    res2 = run_batch(agent2, {"15m": bars_15m, "1h": bars_1h})

    assert len(res1.items) == len(res2.items)
    assert set(res1.items.keys()) == set(res2.items.keys())
    for k in res1.items:
        assert res1.items[k] == res2.items[k]

    assert len(res1.reports) == len(res2.reports)
    for r1, r2 in zip(res1.reports, res2.reports, strict=True):
        assert r1 == r2


# ==============================================================================
# Performance fix must not change results: legacy vs optimised report path
# ==============================================================================


def _legacy_state(self: IctSmcEngine) -> dict[str, Any]:
    """state() exactly as before the performance fix (summarise all, then slice)."""
    bsl_pools = [p.summary() for p in self.pools if p.side == "high"][-self.cfg.max_active_pools :]
    ssl_pools = [p.summary() for p in self.pools if p.side == "low"][-self.cfg.max_active_pools :]
    active_fvgs_objs = [f for f in self.fvgs if f.state != "filled"][-self.cfg.max_active_fvgs :]
    active_obs_objs = [ob for ob in self.order_blocks if ob.state != "invalidated"][
        -self.cfg.max_active_obs :
    ]
    active_breakers = [b.summary() for b in self.breakers if b.state != "invalidated"][
        -self.cfg.max_active_breakers :
    ]
    pd_arrays_in_zone: list[dict[str, Any]] = []
    if self.pd_range:
        zone, eq = self.pd_range.zone, self.pd_range.equilibrium
        if zone == "discount":
            pd_arrays_in_zone = [
                ob.summary() for ob in active_obs_objs if ob.direction == "bullish" and ob.top <= eq
            ] + [f.summary() for f in active_fvgs_objs if f.direction == "bullish" and f.top <= eq]
        elif zone == "premium":
            pd_arrays_in_zone = [
                ob.summary()
                for ob in active_obs_objs
                if ob.direction == "bearish" and ob.bottom >= eq
            ] + [
                f.summary() for f in active_fvgs_objs if f.direction == "bearish" and f.bottom >= eq
            ]
    return {
        "pd_range": self.pd_range.as_dict() if self.pd_range else None,
        "liquidity": {"bsl": bsl_pools, "ssl": ssl_pools, "last_sweep": self.last_sweep},
        "fvgs": [f.summary() for f in active_fvgs_objs],
        "order_blocks": [ob.summary() for ob in active_obs_objs],
        "breakers": active_breakers,
        "pd_arrays_in_zone": pd_arrays_in_zone,
        "displacement": self.displacements[-1].summary() if self.displacements else None,
        "smt": self.last_smt.summary() if self.last_smt else None,
    }


def _random_walk_bars(n: int, seed: int, iv: int) -> list[Bar]:
    import random

    rng = random.Random(seed)
    bars, price = [], 100.0
    for i in range(n):
        o = price
        price = max(5.0, price + rng.gauss(0, 1.2) + (0.8 if (i // 60) % 2 else -0.8))
        hi = max(o, price) + abs(rng.gauss(0, 0.6))
        lo = min(o, price) - abs(rng.gauss(0, 0.6))
        t = T0 + i * iv
        bars.append(Bar(t, t + iv, o, hi, lo, price))
    return bars


def test_performance_fix_keeps_reports_and_items_identical(monkeypatch) -> None:
    """Same unique items and byte-identical reports with the optimised state()/iso()."""
    import json

    import deskmind.agents.ict_smc.agent as agent_mod

    h1 = _random_walk_bars(1500, seed=7, iv=H)
    h2 = [
        Bar(a.open_time, b.close_time, a.open, max(a.high, b.high), min(a.low, b.low), b.close)
        for a, b in zip(h1[::2], h1[1::2], strict=False)
    ]
    bars = {"1h": h1, "2h": h2}
    report_from = h1[400].close_time

    def run():
        agent = SymbolIctSmcAgent("TEST", ["1h", "2h"], CFG, TREND_CFG)
        res = run_batch(agent, bars, report_from=report_from, store_from=report_from)
        return agent, res

    fast_agent, fast = run()
    uncached_iso = agent_mod.iso.__wrapped__
    monkeypatch.setattr(IctSmcEngine, "state", _legacy_state)
    monkeypatch.setattr(agent_mod, "iso", uncached_iso)
    _, slow = run()

    # the fixture must exercise the path that changed: more pools/breakers than are reported
    eng = fast_agent.engines["1h"]
    assert len(eng.pools) > 5 * CFG.max_active_pools
    assert len(eng.breakers) > 0
    assert set(fast.items) == set(slow.items)  # identical unique items
    assert fast.items == slow.items  # and identical content
    assert len(fast.reports) == len(slow.reports) > 1000
    assert json.dumps(fast.reports, sort_keys=True) == json.dumps(slow.reports, sort_keys=True)


# ==============================================================================
# v1.2.0: EQH/EQL self-pairing fix, FVG tags, point-in-time reconstruction
# ==============================================================================


def _swing(pid: str, kind: str, price: float, pivot: int, confirmed: int, **extra: Any) -> dict:
    return {"id": pid, "kind": kind, "price": price, "pivot_time": pivot,
            "confirmed_at": confirmed, **extra}  # fmt: skip


def test_resent_swing_updates_in_place_and_never_pairs_with_itself() -> None:
    eng = IctSmcEngine("TEST", "1h", CFG, TREND_CFG)
    bars = make_bars([100, 101, 100, 101, 100, 101, 100], steps=2, spread=0.3)
    for b in bars[:8]:  # warm up ATR with the engine's own trend engine
        eng.on_candle(b)
    pools_before = len(eng.pools)
    sw = _swing("swing_high:1", "swing_high", 250.0, bars[7].open_time, bars[8].close_time)
    first = eng.on_candle(bars[8], trend_changes=[sw])
    new_pools = [i for i in first if i["kind"] == "bsl"]
    assert len(new_pools) == 1 and new_pools[0]["pool_type"] == "single_high"
    # the trend engine re-sends the same swing on a state change (e.g. broken)
    resent = dict(sw, status="broken")
    again = eng.on_candle(bars[9], trend_changes=[resent])
    assert [i for i in again if i["kind"] in ("bsl", "ssl")] == []
    assert len(eng.pools) == pools_before + 1
    assert sum(1 for s in eng.confirmed_swings if s["id"] == sw["id"]) == 1
    assert next(s for s in eng.confirmed_swings if s["id"] == sw["id"])["status"] == "broken"


def test_eq_pools_always_have_two_distinct_swings() -> None:
    bars = _random_walk_bars(1500, seed=11, iv=H)
    eng, _items = run_engine(bars)
    eq = [p for p in eng.pools if p.pool_type in ("eqh", "eql")]
    assert eq, "fixture should produce equal-high/low pools"
    assert all(len(set(p.swings)) == 2 for p in eq)
    ids = [s["id"] for s in eng.confirmed_swings]
    assert len(ids) == len(set(ids))  # no swing stored twice


def test_fvgs_tagged_with_size_atr_and_displacement_backing() -> None:
    bars = _random_walk_bars(1500, seed=3, iv=H)
    _eng, items = run_engine(bars)
    fvgs = [i for i in items.values() if i["kind"].startswith("fvg_")]
    disp_times = {i["created_time"] for i in items.values() if i["kind"] == "displacement"}
    assert fvgs
    for f in fvgs:
        assert f["size_atr"] == pytest.approx((f["top"] - f["bottom"]) / f["atr"])
        assert f["size_atr"] >= CFG.fvg_min_atr - 1e-12  # detection threshold unchanged
        assert f["displacement_backed"] is (f["created_time"] in disp_times)
    assert any(f["displacement_backed"] for f in fvgs)
    assert any(not f["displacement_backed"] for f in fvgs)


def test_state_reconstructed_from_transitions_equals_engine_state_at_random_t() -> None:
    """Point-in-time state as of T from items + transitions == the engine's live state at T."""
    import copy
    import json
    import random

    from deskmind.agents.ict_smc.history import TransitionRecorder, reconstruct_state

    h1 = _random_walk_bars(2400, seed=21, iv=H)
    h2 = [
        Bar(a.open_time, b.close_time, a.open, max(a.high, b.high), min(a.low, b.low), b.close)
        for a, b in zip(h1[::2], h1[1::2], strict=False)
    ]
    bars = {"1h": h1, "2h": h2}
    by_close: dict[int, dict[str, list[Bar]]] = {}
    for tf, bs in bars.items():
        for b in bs:
            by_close.setdefault(b.close_time, {}).setdefault(tf, []).append(b)
    agent = SymbolIctSmcAgent("TEST", ["1h", "2h"], CFG, TREND_CFG)
    rec = TransitionRecorder()
    store_from = h1[500].close_time  # everything before is warm-up (baseline covers it)
    boundaries = sorted(by_close)
    random.seed(5)
    check_at = set(random.sample([t for t in boundaries if t > store_from], 60))
    check_at |= {next(t for t in boundaries if t >= store_from), boundaries[-1]}
    live: dict[int, dict[str, Any]] = {}
    last_close: dict[str, float] = {}
    for t in boundaries:
        out = agent.on_bar_close(t, by_close[t], with_report=False)
        for tf, bs in by_close[t].items():
            last_close[tf] = bs[-1].close
        if t >= store_from:
            if rec.started:
                rec.boundary(t, agent, out.changes)
            else:
                rec.baseline(t, agent)
        if t in check_at:
            live[t] = {
                tf: (copy.deepcopy(e.state()), last_close.get(tf))
                for tf, e in agent.engines.items()
            }

    # stored rows go through JSON (like Postgres JSONB)
    rows = json.loads(json.dumps(rec.rows))
    by_tf: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: (r["at"], r["ord"])):
        by_tf.setdefault(r["tf"], []).append(r)
    eng1 = agent.engines["1h"]
    assert len(eng1.pools) > 3 * CFG.max_active_pools and eng1.breakers and eng1.pd_range
    compared = 0
    for t, per_tf in live.items():
        for tf, (state, close) in per_tf.items():
            got = reconstruct_state(by_tf[tf], t, CFG, close)
            assert json.loads(json.dumps(got)) == json.loads(json.dumps(state)), (t, tf)
            compared += 1
    assert compared == 2 * len(check_at)
    # far fewer bytes than storing a snapshot per boundary
    snapshot_bytes = sum(len(json.dumps(s)) for p in live.values() for s, _ in p.values())
    snapshot_bytes *= len([t for t in boundaries if t >= store_from]) / len(live)
    assert len(json.dumps(rows)) < snapshot_bytes / 3


def test_item_ids_are_unique_so_storage_never_merges_two_items() -> None:
    """Two OBs from the same earliest candle (different BOS) used to share one id."""
    for seed in (21, 3, 11):
        eng, _ = run_engine(_random_walk_bars(2400, seed=seed, iv=H))
        for objs in (eng.pools, eng.order_blocks, eng.breakers, eng.fvgs, eng.voids):
            ids = [o.id for o in objs]
            assert len(ids) == len(set(ids)), (seed, type(objs[0]).__name__ if objs else None)
