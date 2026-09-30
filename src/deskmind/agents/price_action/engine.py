"""Price Action engine: S/R zones, candle patterns, ranges, breakouts.

Reuses Trend agent swings (passed in via `on_candle`). Never re-detects swings.
No look-ahead: `confirmed_at` = close of the candle that made the item knowable.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from deskmind.agents.price_action.config import (
    DEFAULT_CONFIG,
    ROUND_NUMBER_STEPS,
    PriceActionConfig,
)
from deskmind.agents.trend.models import Bar


# ------------------------------------------------------------------ helpers

def _body(bar: Bar) -> float:
    return abs(bar.close - bar.open)


def _range(bar: Bar) -> float:
    return bar.high - bar.low


def _upper_wick(bar: Bar) -> float:
    return bar.high - max(bar.open, bar.close)


def _lower_wick(bar: Bar) -> float:
    return min(bar.open, bar.close) - bar.low


def _round_step(symbol: str, cfg: PriceActionConfig) -> float:
    if cfg.round_number_step > 0:
        return cfg.round_number_step
    for key, step in ROUND_NUMBER_STEPS.items():
        if key in symbol.upper():
            return step
    return 0.0  # disabled


def _zone_id(side: str, pivot_times: list[int]) -> str:
    joined = "_".join(str(t) for t in sorted(pivot_times))
    return f"sr_{side}_{joined}"


# ------------------------------------------------------------------ S/R zone

class SRZone:
    def __init__(
        self,
        zone_id: str,
        side: str,
        top: float,
        bottom: float,
        pivot_times: list[int],
        created_time: int,
        confirmed_at: int,
    ) -> None:
        self.id = zone_id
        self.side = side           # "support" | "resistance"
        self.top = top
        self.bottom = bottom
        self.mid = (top + bottom) / 2
        self.pivot_times = list(pivot_times)
        self.created_time = created_time
        self.confirmed_at = confirmed_at
        self.state = "active"
        self.strength = 0
        self.touches: list[int] = []    # bar close times of zone touches
        self.broken_at: int | None = None
        self.flipped_at: int | None = None
        self.retired_at: int | None = None
        self.last_touch_bar = 0         # bar index of last interaction

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": f"sr_{self.side}",
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "side": self.side,
            "top": self.top,
            "bottom": self.bottom,
            "mid": self.mid,
            "pivot_times": self.pivot_times,
            "state": self.state,
            "strength": self.strength,
            "touches": list(self.touches),
            "broken_at": self.broken_at,
            "flipped_at": self.flipped_at,
            "retired_at": self.retired_at,
            "rule": "price_action S/R zone (spec §1)",
        }

    def compute_strength(self, atr: float | None, current_bar_idx: int) -> int:
        if not atr or atr <= 0:
            return 0
        score = min(len(self.touches) * 10, 40)
        # Rejections are approximated from touches where state stayed active
        # (simplified: +5 per extra touch beyond 2)
        score += max(0, min(len(self.touches) - 2, 3) * 5)
        bars_since = max(0, current_bar_idx - self.last_touch_bar)
        score -= bars_since // 100
        return max(0, min(score, 100))


# ------------------------------------------------------------------ range

class PriceRange:
    def __init__(self, range_id: str, high: float, low: float, start_time: int,
                 created_time: int, confirmed_at: int) -> None:
        self.id = range_id
        self.high = high
        self.low = low
        self.start_time = start_time
        self.created_time = created_time
        self.confirmed_at = confirmed_at
        self.state: str = "active"
        self.end_time: int | None = None
        self.breakout_direction: str | None = None
        self.bar_count = 1

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "range",
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "range_high": self.high,
            "range_low": self.low,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "state": self.state,
            "breakout_direction": self.breakout_direction,
            "bar_count": self.bar_count,
            "rule": "price_action range (spec §5)",
        }


# ------------------------------------------------------------------ breakout

class Breakout:
    def __init__(
        self, bo_id: str, zone_id: str, breakout_time: int,
        direction: str, confirmed_at: int, created_time: int
    ) -> None:
        self.id = bo_id
        self.zone_id = zone_id
        self.breakout_time = breakout_time
        self.direction = direction        # "up" | "down"
        self.created_time = created_time
        self.confirmed_at = confirmed_at
        self.retest_time: int | None = None
        self.outcome: str | None = None
        self.bars_since = 0

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "breakout",
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "zone_id": self.zone_id,
            "breakout_time": self.breakout_time,
            "direction": self.direction,
            "retest_time": self.retest_time,
            "outcome": self.outcome,
            "rule": "price_action breakout (spec §3)",
        }


# ------------------------------------------------------------------ engine

class PriceActionEngine:
    """Incremental price action engine for one (symbol, timeframe).

    Reuses Trend agent's swing highs/lows via `on_candle(bar, swings=...)`.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        cfg: PriceActionConfig = DEFAULT_CONFIG,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.cfg = cfg
        self.bars: deque[Bar] = deque(maxlen=cfg.bar_memory)
        self.bar_index = 0

        self._atr: float | None = None
        self._tr_buf: deque[float] = deque(maxlen=cfg.atr_period)

        # Swing extremes for S/R clustering (list of (price, confirmed_at))
        self.swing_highs: deque[tuple[float, int]] = deque(maxlen=50)
        self.swing_lows: deque[tuple[float, int]] = deque(maxlen=50)

        self.zones: dict[str, SRZone] = {}
        self.breakouts: list[Breakout] = []
        self.active_range: PriceRange | None = None
        self.range_buf: deque[Bar] = deque(maxlen=cfg.range_max_bars)
        self.patterns: deque[dict[str, Any]] = deque(maxlen=200)
        self.round_numbers: list[float] = []
        self.as_of: int | None = None

    # ---------------------------------------------------------------- ATR

    def _update_atr(self, bar: Bar) -> None:
        if len(self.bars) >= 2:
            prev = list(self.bars)[-2]
            tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        else:
            tr = bar.high - bar.low
        self._tr_buf.append(tr)
        if len(self._tr_buf) >= self.cfg.atr_period:
            self._atr = sum(self._tr_buf) / len(self._tr_buf)

    # ---------------------------------------------------------------- main

    def on_candle(
        self,
        bar: Bar,
        swings: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Process one closed candle. `swings` = new swing items from Trend agent."""
        if self.bars and bar.open_time <= self.bars[-1].open_time:
            return []
        self._update_atr(bar)
        self.bars.append(bar)
        self.bar_index += 1
        atr = self._atr
        changes: list[dict[str, Any]] = []

        if swings:
            for sw in swings:
                kind = sw.get("kind", "")
                if kind == "swing_high":
                    self.swing_highs.append((sw["price"], sw["confirmed_at"]))
                elif kind == "swing_low":
                    self.swing_lows.append((sw["price"], sw["confirmed_at"]))
            changes.extend(self._update_sr_zones(bar, atr))

        if atr is not None:
            changes.extend(self._update_zone_states(bar, atr))
            changes.extend(self._detect_pattern(bar, atr))
            changes.extend(self._update_ranges(bar, atr))
            changes.extend(self._update_breakouts(bar, atr))
            self._maybe_update_round_numbers(bar)

        self.as_of = bar.close_time
        return changes

    # ---------------------------------------------------------------- S/R zones

    def _update_sr_zones(self, bar: Bar, atr: float | None) -> list[dict[str, Any]]:
        if atr is None or atr <= 0:
            return []
        tol = self.cfg.sr_cluster_atr * atr
        changes: list[dict[str, Any]] = []

        for swings, side in [(self.swing_highs, "resistance"), (self.swing_lows, "support")]:
            # Group into clusters
            sorted_swings = sorted(swings, key=lambda x: x[0])
            clusters: list[list[tuple[float, int]]] = []
            for price, ct in sorted_swings:
                if clusters and abs(price - clusters[-1][-1][0]) <= tol:
                    clusters[-1].append((price, ct))
                else:
                    clusters.append([(price, ct)])
            for cluster in clusters:
                if len(cluster) < self.cfg.sr_min_touches:
                    continue
                prices = [p for p, _ in cluster]
                times = [ct for _, ct in cluster]
                mid = sum(prices) / len(prices)
                half_w = max(self.cfg.sr_zone_width_atr * atr, (max(prices) - min(prices)) / 2)
                top = mid + half_w
                bottom = mid - half_w
                zone_id = _zone_id(side, times)
                if zone_id in self.zones:
                    continue
                zone = SRZone(
                    zone_id=zone_id,
                    side=side,
                    top=top,
                    bottom=bottom,
                    pivot_times=times,
                    created_time=min(times),
                    confirmed_at=max(times),
                )
                zone.touches = list(times)
                zone.strength = zone.compute_strength(atr, self.bar_index)
                self.zones[zone_id] = zone
                changes.append(zone.item())
        return changes

    # ---------------------------------------------------------------- zone state updates

    def _update_zone_states(self, bar: Bar, atr: float) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for zone in list(self.zones.values()):
            if zone.state in ("retired",):
                continue

            price_in_zone = zone.bottom <= bar.low <= zone.top or zone.bottom <= bar.high <= zone.top
            close_in_zone = zone.bottom <= bar.close <= zone.top

            if price_in_zone:
                zone.touches.append(bar.close_time)
                zone.last_touch_bar = self.bar_index
                zone.strength = zone.compute_strength(atr, self.bar_index)
                changes.append(zone.item())

            # Breakout check
            if zone.state == "active" and not close_in_zone:
                if zone.side == "resistance" and bar.close > zone.top + self.cfg.bo_min_atr * atr:
                    zone.state = "broken"
                    zone.broken_at = bar.close_time
                    changes.append(zone.item())
                    bo = Breakout(
                        bo_id=f"bo_{zone.id}_{bar.close_time}",
                        zone_id=zone.id,
                        breakout_time=bar.close_time,
                        direction="up",
                        confirmed_at=bar.close_time,
                        created_time=bar.open_time,
                    )
                    self.breakouts.append(bo)
                    changes.append(bo.item())
                elif zone.side == "support" and bar.close < zone.bottom - self.cfg.bo_min_atr * atr:
                    zone.state = "broken"
                    zone.broken_at = bar.close_time
                    changes.append(zone.item())
                    bo = Breakout(
                        bo_id=f"bo_{zone.id}_{bar.close_time}",
                        zone_id=zone.id,
                        breakout_time=bar.close_time,
                        direction="down",
                        confirmed_at=bar.close_time,
                        created_time=bar.open_time,
                    )
                    self.breakouts.append(bo)
                    changes.append(bo.item())

            # Flip check (broken zone re-tested from other side)
            elif zone.state == "broken":
                if zone.side == "resistance" and close_in_zone and bar.close < bar.open:
                    zone.state = "flipped"
                    zone.flipped_at = bar.close_time
                    changes.append(zone.item())
                elif zone.side == "support" and close_in_zone and bar.close > bar.open:
                    zone.state = "flipped"
                    zone.flipped_at = bar.close_time
                    changes.append(zone.item())

            # Age-out check
            if self.bar_index - zone.last_touch_bar > self.cfg.sr_max_age_bars and zone.state != "retired":
                zone.state = "retired"
                zone.retired_at = bar.close_time
                changes.append(zone.item())
        return changes

    # ---------------------------------------------------------------- breakouts

    def _update_breakouts(self, bar: Bar, atr: float) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for bo in self.breakouts:
            if bo.outcome is not None:
                continue
            bo.bars_since += 1
            if bo.bars_since > self.cfg.bo_retest_max_bars:
                bo.outcome = "expired"
                changes.append(bo.item())
                continue
            zone = self.zones.get(bo.zone_id)
            if zone is None:
                continue
            in_zone = zone.bottom <= bar.low <= zone.top or zone.bottom <= bar.high <= zone.top
            if not in_zone:
                continue
            # Retest detected
            bo.retest_time = bar.close_time
            if bo.direction == "up" and bar.close >= zone.top:
                bo.outcome = "held"
            elif bo.direction == "down" and bar.close <= zone.bottom:
                bo.outcome = "held"
            else:
                bo.outcome = "failed"
            changes.append(bo.item())
        return changes

    # ---------------------------------------------------------------- candle patterns

    def _detect_pattern(self, bar: Bar, atr: float) -> list[dict[str, Any]]:
        if atr <= 0 or len(self.bars) < 2:
            return []
        bars = list(self.bars)
        prev = bars[-2] if len(bars) >= 2 else None
        p2 = bars[-3] if len(bars) >= 3 else None
        changes: list[dict[str, Any]] = []
        body = _body(bar)
        rng = _range(bar)
        lower = _lower_wick(bar)
        upper = _upper_wick(bar)
        bullish = bar.close > bar.open
        bearish = bar.close < bar.open
        patterns: list[str] = []

        if prev is not None:
            prev_body = _body(prev)
            # Engulfing
            if (
                bullish
                and prev.close < prev.open  # prev bearish
                and bar.open <= prev.close
                and bar.close >= prev.open
                and body > prev_body
            ):
                patterns.append("bullish_engulfing")
            elif (
                bearish
                and prev.close > prev.open  # prev bullish
                and bar.open >= prev.close
                and bar.close <= prev.open
                and body > prev_body
            ):
                patterns.append("bearish_engulfing")

            # Inside / outside bar
            if bar.high < prev.high and bar.low > prev.low:
                patterns.append("inside_bar")
            elif bar.high > prev.high and bar.low < prev.low:
                patterns.append("outside_bar")

            # Doji
            if body <= self.cfg.doji_body_atr * atr:
                patterns.append("doji")

            # Hammer / shooting star
            if body > 0 and lower >= self.cfg.pin_wick_ratio * body and upper <= body * 0.5:
                patterns.append("hammer")
            elif body > 0 and upper >= self.cfg.pin_wick_ratio * body and lower <= body * 0.5:
                patterns.append("shooting_star")

        # 3-candle: morning/evening star
        if p2 is not None and prev is not None:
            p2_body = _body(p2)
            prev_body = _body(prev)
            mid_of_p2 = (p2.open + p2.close) / 2
            if (
                p2.close < p2.open  # P2 bearish
                and prev_body <= 0.3 * p2_body  # small middle
                and bar.close > bar.open  # C bullish
                and bar.close > mid_of_p2
            ):
                patterns.append("morning_star")
            elif (
                p2.close > p2.open  # P2 bullish
                and prev_body <= 0.3 * p2_body
                and bar.close < bar.open  # C bearish
                and bar.close < mid_of_p2
            ):
                patterns.append("evening_star")

        for kind in patterns:
            nearest_zone = self._nearest_zone(bar.close, atr)
            context = "at_zone" if nearest_zone is not None else "open_space"
            item: dict[str, Any] = {
                "id": f"pattern_{kind}:{bar.close_time}",
                "kind": f"pattern_{kind}",
                "created_time": bar.open_time,
                "confirmed_at": bar.close_time,
                "pattern": kind,
                "at_zone": nearest_zone,
                "context": context,
                "size_atr": round(rng / atr, 3) if atr > 0 else None,
                "bullish": bullish,
                "rule": f"price_action candle pattern (spec §4): {kind}",
            }
            self.patterns.append(item)
            changes.append(item)
        return changes

    def _nearest_zone(self, price: float, atr: float) -> str | None:
        tol = self.cfg.zone_tag_atr * atr
        best_dist = float("inf")
        best_id: str | None = None
        for zone in self.zones.values():
            if zone.state == "retired":
                continue
            dist = min(abs(price - zone.top), abs(price - zone.bottom), abs(price - zone.mid))
            if dist < tol and dist < best_dist:
                best_dist = dist
                best_id = zone.id
        return best_id

    # ---------------------------------------------------------------- range detection

    def _update_ranges(self, bar: Bar, atr: float) -> list[dict[str, Any]]:
        self.range_buf.append(bar)
        changes: list[dict[str, Any]] = []
        buf = list(self.range_buf)

        if self.active_range is not None:
            rng = self.active_range
            # Check breakout
            if bar.close > rng.high + 0.01 * atr:
                rng.state = "broken"
                rng.end_time = bar.close_time
                rng.breakout_direction = "up"
                self.active_range = None
                changes.append(rng.item())
            elif bar.close < rng.low - 0.01 * atr:
                rng.state = "broken"
                rng.end_time = bar.close_time
                rng.breakout_direction = "down"
                self.active_range = None
                changes.append(rng.item())
            else:
                rng.bar_count += 1
                rng.high = max(rng.high, bar.high)
                rng.low = min(rng.low, bar.low)
                if rng.bar_count <= self.cfg.range_max_bars:
                    changes.append(rng.item())
                else:
                    rng.state = "retired" if hasattr(rng, "retired") else "broken"
                    self.active_range = None
            return changes

        # Try to detect a new range
        n = len(buf)
        if n < self.cfg.range_min_bars:
            return changes
        for start_i in range(max(0, n - self.cfg.range_max_bars), n - self.cfg.range_min_bars + 1):
            window = buf[start_i:]
            rng_h = max(b.high for b in window)
            rng_l = min(b.low for b in window)
            if (rng_h - rng_l) <= self.cfg.range_width_atr * atr:
                start_bar = window[0]
                pr = PriceRange(
                    range_id=f"range:{start_bar.open_time}",
                    high=rng_h,
                    low=rng_l,
                    start_time=start_bar.open_time,
                    created_time=start_bar.open_time,
                    confirmed_at=bar.close_time,
                )
                pr.bar_count = len(window)
                self.active_range = pr
                changes.append(pr.item())
                break
        return changes

    # ---------------------------------------------------------------- round numbers

    def _maybe_update_round_numbers(self, bar: Bar) -> None:
        step = _round_step(self.symbol, self.cfg)
        if not step:
            return
        # Only recompute if list is empty (or price moved significantly)
        if self.round_numbers:
            return
        # Generate round levels spanning the current price ± 20 steps
        price = bar.close
        base = math.floor(price / step) * step
        self.round_numbers = [
            round(base + step * i, 10) for i in range(-20, 21)
        ]

    # ---------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        atr = self._atr
        active_zones = [z.item() for z in self.zones.values() if z.state != "retired"]
        recent_pats = list(self.patterns)[-10:]
        recent_bos = [bo.item() for bo in self.breakouts if bo.outcome is None]
        active_rng = self.active_range.item() if self.active_range else None

        # Nearest support / resistance
        close = list(self.bars)[-1].close if self.bars else 0.0
        nearest_sup = nearest_res = None
        for zone in sorted(self.zones.values(), key=lambda z: z.mid):
            if zone.state == "retired":
                continue
            if zone.side == "support" and zone.top <= close:
                nearest_sup = zone.item()
            elif zone.side == "resistance" and zone.bottom >= close and nearest_res is None:
                nearest_res = zone.item()
        if nearest_sup is None:
            # Fallback: closest support below
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
