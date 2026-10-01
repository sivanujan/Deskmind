"""Performance and byte-identical regression tests for Phase A agents and feature store."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from deskmind.agents.ict_smc.config import DEFAULT_CONFIG as SMC_CFG
from deskmind.agents.ict_smc.engine import IctSmcEngine
from deskmind.agents.ict_time.config import DEFAULT_CONFIG as ICT_TIME_CFG
from deskmind.agents.ict_time.engine import IctTimeEngine
from deskmind.agents.price_action.config import DEFAULT_CONFIG as PA_CFG
from deskmind.agents.price_action.engine import Breakout, PriceActionEngine, SRZone
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.models import Bar
from deskmind.features.builder import build_feature_row
from deskmind.research.full_run import _load_stored_bars


class UnoptimizedReferencePriceActionEngine(PriceActionEngine):
    """Old implementation that scans all historical zones and breakouts on every bar."""

    def _update_zone_states(self, bar: Bar, atr: float) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for zone in list(self.zones.values()):
            if zone.state in ("retired",):
                continue

            price_in_zone = zone.bottom <= bar.low <= zone.top or zone.bottom <= bar.high <= zone.top
            if price_in_zone:
                zone.touches.append(bar.close_time)
                zone.state = "tested"
                zone.strength = zone.compute_strength(atr, self.bar_index)
                changes.append(zone.item())

            if zone.state in ("fresh", "tested"):
                if zone.side == "resistance" and bar.close > zone.top + self.cfg.bo_min_atr * atr:
                    zone.state = "broken"
                    zone.broken_at = bar.close_time
                    changes.append(zone.item())
                    bo = Breakout(
                        zone_id=zone.id,
                        side="bullish",
                        break_price=bar.close,
                        break_time=bar.close_time,
                        created_time=bar.open_time,
                    )
                    self.breakouts.append(bo)
                    changes.append(bo.item())
                elif zone.side == "support" and bar.close < zone.bottom - self.cfg.bo_min_atr * atr:
                    zone.state = "broken"
                    zone.broken_at = bar.close_time
                    changes.append(zone.item())
                    bo = Breakout(
                        zone_id=zone.id,
                        side="bearish",
                        break_price=bar.close,
                        break_time=bar.close_time,
                        created_time=bar.open_time,
                    )
                    self.breakouts.append(bo)
                    changes.append(bo.item())

            if zone.state == "broken":
                if zone.side == "resistance" and bar.low >= zone.top:
                    zone.state = "flipped"
                    zone.side = "support"
                    zone.flipped_at = bar.close_time
                    changes.append(zone.item())
                elif zone.side == "support" and bar.high <= zone.bottom:
                    zone.state = "flipped"
                    zone.side = "resistance"
                    zone.flipped_at = bar.close_time
                    changes.append(zone.item())

            if self.bar_index - zone.bar_index > self.cfg.zone_max_bars:
                zone.state = "retired"
                zone.retired_at = bar.close_time
                changes.append(zone.item())
        return changes

    def _update_breakouts(self, bar: Bar, atr: float) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for bo in self.breakouts:
            if bo.outcome is not None:
                continue
            bo.bars_since += 1
            if bo.bars_since < self.cfg.bo_retest_min_bars:
                continue
            zone = self.zones.get(bo.zone_id)
            if zone is None:
                continue
            in_zone = zone.bottom <= bar.low <= zone.top or zone.bottom <= bar.high <= zone.top
            if not in_zone:
                continue
            bo.retest_time = bar.close_time
            if bo.side == "bullish" and bar.close > zone.top:
                bo.outcome = "confirmed"
            elif bo.side == "bearish" and bar.close < zone.bottom:
                bo.outcome = "confirmed"
            else:
                bo.outcome = "failed"
            changes.append(bo.item())
        return changes

    def state(self) -> dict[str, Any]:
        atr = self._atr
        active_zones = [z.item() for z in self.zones.values() if z.state != "retired"]
        recent_pats = list(self.patterns)[-10:]
        recent_bos = [bo.item() for bo in self.breakouts if bo.outcome is None]
        active_rng = self.active_range.item() if self.active_range else None

        close = self.bars[-1].close if self.bars else 0.0
        nearest_sup = nearest_res = None
        for zone in sorted(self.zones.values(), key=lambda z: z.mid):
            if zone.state == "retired":
                continue
            if zone.side == "support" and zone.top <= close:
                nearest_sup = zone.item()
            elif zone.side == "resistance" and zone.bottom >= close and nearest_res is None:
                nearest_res = zone.item()
        if nearest_sup is None:
            sups = [z for z in self.zones.values() if z.side == "support" and z.state != "retired"]
            if sups:
                nearest_sup = min(sups, key=lambda z: abs(z.mid - close)).item()
        if nearest_res is None:
            ress = [z for z in self.zones.values() if z.side == "resistance" and z.state != "retired"]
            if ress:
                nearest_res = min(ress, key=lambda z: abs(z.mid - close)).item()

        def enrich(zone_item: dict | None) -> dict | None:
            if zone_item is None or not atr or atr <= 0:
                return zone_item
            d = dict(zone_item)
            d["distance_atr"] = round((d.get("mid", 0) - close) / atr, 3)
            return d

        return {
            "nearest_support": enrich(nearest_sup),
            "nearest_resistance": enrich(nearest_res),
            "active_zones": active_zones,
            "active_range": active_rng,
            "recent_patterns": recent_pats,
            "recent_breakouts": recent_bos,
            "round_numbers": self.round_numbers[:10] if self.round_numbers else [],
            "atr": atr,
        }


class UnoptimizedReferenceIctSmcEngine(IctSmcEngine):
    """Old implementation that scans all historical lists forward on every bar."""

    def state(self) -> dict[str, Any]:
        bsl_pools = [
            p.summary()
            for p in [p for p in self.pools if p.side == "high"][-self.cfg.max_active_pools :]
        ]
        ssl_pools = [
            p.summary()
            for p in [p for p in self.pools if p.side == "low"][-self.cfg.max_active_pools :]
        ]
        active_fvgs_objs = [f for f in self.fvgs if f.state != "filled"][
            -self.cfg.max_active_fvgs :
        ]
        active_obs_objs = [ob for ob in self.order_blocks if ob.state != "invalidated"][
            -self.cfg.max_active_obs :
        ]
        active_fvgs = [f.summary() for f in active_fvgs_objs]
        active_obs = [ob.summary() for ob in active_obs_objs]
        active_breakers = [
            b.summary()
            for b in [b for b in self.breakers if b.state != "invalidated"][
                -self.cfg.max_active_breakers :
            ]
        ]
        pd_arrays_in_zone: list[dict[str, Any]] = []
        if self.pd_range:
            zone = self.pd_range.zone
            eq = self.pd_range.equilibrium
            if zone == "discount":
                pd_arrays_in_zone = [
                    ob.summary()
                    for ob in active_obs_objs
                    if ob.direction == "bullish" and ob.top <= eq
                ] + [
                    f.summary()
                    for f in active_fvgs_objs
                    if f.direction == "bullish" and f.top <= eq
                ]
            elif zone == "premium":
                pd_arrays_in_zone = [
                    ob.summary()
                    for ob in active_obs_objs
                    if ob.direction == "bearish" and ob.bottom >= eq
                ] + [
                    f.summary()
                    for f in active_fvgs_objs
                    if f.direction == "bearish" and f.bottom >= eq
                ]

        last_disp = self.displacements[-1].summary() if self.displacements else None

        return {
            "pd_range": self.pd_range.as_dict() if self.pd_range else None,
            "liquidity": {
                "bsl": bsl_pools,
                "ssl": ssl_pools,
                "last_sweep": self.last_sweep,
            },
            "fvgs": active_fvgs,
            "order_blocks": active_obs,
            "breakers": active_breakers,
            "pd_arrays_in_zone": pd_arrays_in_zone,
            "displacement": last_disp,
            "smt": self.last_smt.summary() if self.last_smt else None,
        }


class UnoptimizedReferenceIctTimeEngine(IctTimeEngine):
    """Old implementation that iterates all historical sessions and key levels on every bar."""

    def state(self) -> dict[str, Any]:
        from deskmind.agents.ict_time.engine import (
            _current_killzone,
            _current_silver_bullet,
            _minutes_to_next_kz,
        )

        close_ms = self.as_of or 0
        atr = self._atr
        sessions_out: dict[str, Any] = {}
        for s in self.sessions.values():
            sessions_out[s.name] = s.summary()

        levels_out = []
        for lv in sorted(self.key_levels.values(), key=lambda l: l.confirmed_at):
            if lv.state == "taken":
                continue
            d: dict[str, Any] = lv.summary()
            if atr and atr > 0:
                close_price = self.bars[-1].close if self.bars else 0.0
                d["distance_atr"] = round((lv.price - close_price) / atr, 3)
            else:
                d["distance_atr"] = None
            levels_out.append(d)

        weekend = False
        if close_ms:
            dt = datetime.fromtimestamp(close_ms / 1000, UTC)
            weekend = dt.weekday() >= 5

        kz = _current_killzone(close_ms) if close_ms else None
        sb = _current_silver_bullet(close_ms) if close_ms else None

        return {
            "sessions": sessions_out,
            "current_session": self._current_session_name(close_ms),
            "killzone": {
                "current": kz,
                "minutes_to_next": _minutes_to_next_kz(close_ms) if close_ms else None,
            },
            "silver_bullet": {
                "active": sb,
                "fvg_in_windows": [],
            },
            "key_levels": levels_out,
            "weekend_flag": weekend,
            "amd": {
                "phase": self._amd_phase,
                "confidence": self._amd_confidence,
                "heuristic": True,
            },
        }


def _generate_synthetic_fixture(n_bars: int = 3000) -> list[Bar]:
    """Generate realistic synthetic bars spanning several days to test sessions and swings."""
    base_ts = 1672531200000  # 2023-01-01 00:00:00 UTC
    bars: list[Bar] = []
    p = 100.0
    for i in range(n_bars):
        ot = base_ts + i * 300_000
        ct = ot + 300_000
        # Oscillate to generate swings, breakouts, and sessions
        step = 0.5 if (i // 15) % 2 == 0 else -0.5
        p += step
        bars.append(
            Bar(
                open_time=ot,
                close_time=ct,
                open=p - 0.2,
                high=p + 0.8,
                low=p - 0.8,
                close=p,
            )
        )
    return bars


def test_fixture_byte_identical() -> None:
    """Requirement 3: Results must stay byte-identical on a fixture of a few thousand bars."""
    bars = _generate_synthetic_fixture(3000)

    pa_new = PriceActionEngine("BTCUSDT", "5m")
    pa_old = UnoptimizedReferencePriceActionEngine("BTCUSDT", "5m")

    smc_new = IctSmcEngine("BTCUSDT", "5m")
    smc_old = UnoptimizedReferenceIctSmcEngine("BTCUSDT", "5m")

    time_new = IctTimeEngine("BTCUSDT")
    time_old = UnoptimizedReferenceIctTimeEngine("BTCUSDT")

    trend_engine = TrendEngine("BTCUSDT", "5m")

    for i, bar in enumerate(bars):
        chg_pa_new = pa_new.on_candle(bar)
        chg_pa_old = pa_old.on_candle(bar)
        assert chg_pa_new == chg_pa_old, f"PA changes mismatch at bar {i}"

        chg_smc_new = smc_new.on_candle(bar)
        chg_smc_old = smc_old.on_candle(bar)
        assert chg_smc_new == chg_smc_old, f"SMC changes mismatch at bar {i}"

        chg_time_new = time_new.on_candle(bar)
        chg_time_old = time_old.on_candle(bar)
        assert chg_time_new == chg_time_old, f"Time changes mismatch at bar {i}"

        # State identity
        st_pa_new = pa_new.state()
        st_pa_old = pa_old.state()
        assert st_pa_new == st_pa_old, f"PA state mismatch at bar {i}"

        st_smc_new = smc_new.state()
        st_smc_old = smc_old.state()
        assert st_smc_new == st_smc_old, f"SMC state mismatch at bar {i}"

        st_time_new = time_new.state()
        st_time_old = time_old.state()
        assert st_time_new == st_time_old, f"Time state mismatch at bar {i}"

        # Feature row identity
        trend_engine.on_candle(bar)
        t_st = trend_engine.state()
        row_new = build_feature_row(
            symbol="BTCUSDT",
            timestamp=bar.close_time,
            current_close=bar.close,
            trend_state_2h=t_st,
            trend_state_1h=t_st,
            trend_state_15m=t_st,
            trend_state_5m=t_st,
            ict_smc_state_1h=st_smc_new,
            ict_smc_state_15m=st_smc_new,
            ict_smc_state_5m=st_smc_new,
            ict_time_state=st_time_new,
            price_action_state_1h=st_pa_new,
            price_action_state_5m=st_pa_new,
        )
        row_old = build_feature_row(
            symbol="BTCUSDT",
            timestamp=bar.close_time,
            current_close=bar.close,
            trend_state_2h=t_st,
            trend_state_1h=t_st,
            trend_state_15m=t_st,
            trend_state_5m=t_st,
            ict_smc_state_1h=st_smc_old,
            ict_smc_state_15m=st_smc_old,
            ict_smc_state_5m=st_smc_old,
            ict_time_state=st_time_old,
            price_action_state_1h=st_pa_old,
            price_action_state_5m=st_pa_old,
        )
        assert row_new == row_old, f"Feature row mismatch at bar {i}"


def test_real_data_adausdt_1_month_byte_identical() -> None:
    """Requirement 3: Results must stay byte-identical on 1 month of real ADAUSDT data."""
    bars_5m = _load_stored_bars(
        "data", "ADAUSDT", "5m", datetime(2022, 12, 1, tzinfo=UTC), datetime(2023, 2, 1, tzinfo=UTC)
    )
    if not bars_5m:
        pytest.skip("ADAUSDT real bars not available in data/")

    bars_5m = sorted({b.open_time: b for b in bars_5m}.values(), key=lambda b: b.open_time)
    bars_subset = bars_5m[:17856]  # Warmup + 1 Month (Dec 2022 - Jan 2023)

    pa_new = PriceActionEngine("ADAUSDT", "5m")
    pa_old = UnoptimizedReferencePriceActionEngine("ADAUSDT", "5m")

    smc_new = IctSmcEngine("ADAUSDT", "5m")
    smc_old = UnoptimizedReferenceIctSmcEngine("ADAUSDT", "5m")

    time_new = IctTimeEngine("ADAUSDT")
    time_old = UnoptimizedReferenceIctTimeEngine("ADAUSDT")

    trend_engine = TrendEngine("ADAUSDT", "5m")

    for i, bar in enumerate(bars_subset):
        assert pa_new.on_candle(bar) == pa_old.on_candle(bar), f"PA mismatch at {i}"
        assert smc_new.on_candle(bar) == smc_old.on_candle(bar), f"SMC mismatch at {i}"
        assert time_new.on_candle(bar) == time_old.on_candle(bar), f"Time mismatch at {i}"

        # Periodic deep state and feature row check
        if (i + 1) % 50 == 0 or i == len(bars_subset) - 1:
            st_pa_new = pa_new.state()
            st_pa_old = pa_old.state()
            assert st_pa_new == st_pa_old, f"PA state mismatch at {i}"

            st_smc_new = smc_new.state()
            st_smc_old = smc_old.state()
            assert st_smc_new == st_smc_old, f"SMC state mismatch at {i}"

            st_time_new = time_new.state()
            st_time_old = time_old.state()
            assert st_time_new == st_time_old, f"Time state mismatch at {i}"

            trend_engine.on_candle(bar)
            t_st = trend_engine.state()
            row_new = build_feature_row(
                symbol="ADAUSDT",
                timestamp=bar.close_time,
                current_close=bar.close,
                trend_state_2h=t_st,
                trend_state_1h=t_st,
                trend_state_15m=t_st,
                trend_state_5m=t_st,
                ict_smc_state_1h=st_smc_new,
                ict_smc_state_15m=st_smc_new,
                ict_smc_state_5m=st_smc_new,
                ict_time_state=st_time_new,
                price_action_state_1h=st_pa_new,
                price_action_state_5m=st_pa_new,
            )
            row_old = build_feature_row(
                symbol="ADAUSDT",
                timestamp=bar.close_time,
                current_close=bar.close,
                trend_state_2h=t_st,
                trend_state_1h=t_st,
                trend_state_15m=t_st,
                trend_state_5m=t_st,
                ict_smc_state_1h=st_smc_old,
                ict_smc_state_15m=st_smc_old,
                ict_smc_state_5m=st_smc_old,
                ict_time_state=st_time_old,
                price_action_state_1h=st_pa_old,
                price_action_state_5m=st_pa_old,
            )
            assert row_new == row_old, f"Feature row mismatch at {i}"


def test_performance_regression_linear_scaling() -> None:
    """Requirement 4: per-bar time on bars 40k-50k must not exceed 1.5x the time on bars 0-10k."""
    bars_5m = _load_stored_bars(
        "data", "ADAUSDT", "5m", datetime(2022, 12, 1, tzinfo=UTC), datetime(2023, 7, 1, tzinfo=UTC)
    )
    if not bars_5m or len(bars_5m) < 50000:
        bars_5m = _generate_synthetic_fixture(50000)
    else:
        bars_5m = sorted({b.open_time: b for b in bars_5m}.values(), key=lambda b: b.open_time)[:50000]

    pa_engine = PriceActionEngine("ADAUSDT", "5m")
    smc_engine = IctSmcEngine("ADAUSDT", "5m")
    time_engine = IctTimeEngine("ADAUSDT")
    trend_engine = TrendEngine("ADAUSDT", "5m")

    t_0_10k = 0.0
    t_40_50k = 0.0

    for i, bar in enumerate(bars_5m):
        t0 = time.perf_counter()

        pa_engine.on_candle(bar)
        smc_engine.on_candle(bar)
        time_engine.on_candle(bar)
        trend_engine.on_candle(bar)

        # Generate state periodically as happens in the real pipeline
        if (i + 1) % 5 == 0:
            st_pa = pa_engine.state()
            st_smc = smc_engine.state()
            st_time = time_engine.state()
            st_tr = trend_engine.state()
            build_feature_row(
                symbol="ADAUSDT",
                timestamp=bar.close_time,
                current_close=bar.close,
                trend_state_2h=st_tr,
                trend_state_1h=st_tr,
                trend_state_15m=st_tr,
                trend_state_5m=st_tr,
                ict_smc_state_1h=st_smc,
                ict_smc_state_15m=st_smc,
                ict_smc_state_5m=st_smc,
                ict_time_state=st_time,
                price_action_state_1h=st_pa,
                price_action_state_5m=st_pa,
            )

        dt = time.perf_counter() - t0

        if i < 10000:
            t_0_10k += dt
        elif 40000 <= i < 50000:
            t_40_50k += dt

    per_bar_0_10k = (t_0_10k / 10000) * 1000  # ms
    per_bar_40_50k = (t_40_50k / 10000) * 1000  # ms
    ratio = per_bar_40_50k / per_bar_0_10k if per_bar_0_10k > 0 else 1.0

    print(f"\n[Perf Regression Test] Bars 0-10k: {per_bar_0_10k:.4f} ms/bar | Bars 40k-50k: {per_bar_40_50k:.4f} ms/bar | Ratio: {ratio:.3f}x")
    assert ratio <= 1.5, (
        f"Performance regression detected: bars 40k-50k latency ({per_bar_40_50k:.4f} ms/bar) "
        f"exceeds 1.5x of bars 0-10k latency ({per_bar_0_10k:.4f} ms/bar). Ratio: {ratio:.3f}x > 1.5x"
    )
