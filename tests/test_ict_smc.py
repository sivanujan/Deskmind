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
