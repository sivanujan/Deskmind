"""Incremental trend & structure engine for one (symbol, timeframe). See docs/agents/trend.md.

Feed closed candles strictly in time order with `on_candle`. The engine only ever sees
candles that have closed, so it cannot look ahead; every item records `confirmed_at` = the close
time of the candle at which it became knowable. The same class drives live processing,
history backfill and tests.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from deskmind.agents.trend.config import DEFAULT_CONFIG, TrendConfig
from deskmind.agents.trend.models import Bar, Break, Sweep, Swing, Touch, TrendLine


class TrendEngine:
    def __init__(self, symbol: str, timeframe: str, cfg: TrendConfig = DEFAULT_CONFIG) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.cfg = cfg
        self.bars: deque[Bar] = deque(maxlen=cfg.bar_memory)
        self.n = 0  # candles processed; index of the next candle
        self.trs: deque[float] = deque(maxlen=cfg.atr_period)
        self.atr: float | None = None
        self.swings: deque[Swing] = deque(maxlen=200)
        self.last_high: Swing | None = None
        self.last_low: Swing | None = None
        self.level_high: Swing | None = None  # last accepted, unbroken
        self.level_low: Swing | None = None
        self.last_accepted_idx: int | None = None
        self.trend = "ranging"
        self.reason = "no_break_yet"
        self.last_break: Break | None = None
        self.last_break_idx: int | None = None
        self.lines: list[TrendLine] = []  # active (unbroken, unexpired)
        self.line_ids: set[str] = set()  # every line ever created: ids are never reused
        self.swept_swings: set[int] = set()  # swing pivot_times that have already emitted a sweep
        self.as_of: int | None = None

    # ------------------------------------------------------------ helpers

    @property
    def last_open_time(self) -> int | None:
        return self.bars[-1].open_time if self.bars else None

    def _bar(self, idx: int) -> Bar | None:
        first = self.n - len(self.bars)
        return self.bars[idx - first] if first <= idx < self.n else None

    # ------------------------------------------------------------ main entry

    def on_candle(self, bar: Bar) -> list[dict[str, Any]]:
        """Process one CLOSED candle. Returns new/updated items (full item dicts)."""
        if self.bars and bar.open_time <= self.bars[-1].open_time:
            return []  # duplicate or out of order: already processed
        prev_close = self.bars[-1].close if self.bars else None
        i = self.n
        self.bars.append(bar)
        self.n += 1
        tr = (
            bar.high - bar.low
            if prev_close is None
            else max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        )
        self.trs.append(tr)
        self.atr = sum(self.trs) / len(self.trs) if len(self.trs) == self.cfg.atr_period else None

        changes: list[dict[str, Any]] = []
        self._check_breaks(i, bar, changes)
        self._check_lines(i, bar, changes)
        self._check_pivots(i, bar, changes)
        if (
            self.trend != "ranging"
            and self.last_break_idx is not None
            and i - self.last_break_idx >= self.cfg.range_after_bars
        ):
            self.trend = "ranging"
            self.reason = f"no_break_for_{self.cfg.range_after_bars}_bars"
        self.as_of = bar.close_time
        return changes

    # ------------------------------------------------------------ §4 breaks and sweeps

    def _check_breaks(self, i: int, bar: Bar, changes: list[dict[str, Any]]) -> None:
        for direction in ("up", "down"):
            level = self.level_high if direction == "up" else self.level_low
            if level is None:
                continue
            wick = bar.high > level.price if direction == "up" else bar.low < level.price
            if not wick:
                continue
            closed = bar.close > level.price if direction == "up" else bar.close < level.price
            if not closed:
                if level.pivot_time not in self.swept_swings:
                    sweep = Sweep(
                        direction=direction,
                        level=level.price,
                        extreme=bar.high if direction == "up" else bar.low,
                        close=bar.close,
                        time=bar.open_time,
                        confirmed_at=bar.close_time,
                        swing_pivot_time=level.pivot_time,
                    )
                    self.swept_swings.add(level.pivot_time)
                    changes.append(sweep.item())
                continue
            with_trend = self.trend == "ranging" or self.trend == (
                "bullish" if direction == "up" else "bearish"
            )
            brk = Break(
                type="BOS" if with_trend else "CHoCH",
                direction=direction,
                level=level.price,
                time=bar.open_time,
                confirmed_at=bar.close_time,
                swing_pivot_time=level.pivot_time,
                close=bar.close,
            )
            level.broken_at = bar.close_time
            changes.append(level.item())
            changes.append(brk.item())
            if direction == "up":
                self.level_high = None
            else:
                self.level_low = None
            self.trend = "bullish" if direction == "up" else "bearish"
            self.reason = f"{brk.type.lower()}_{direction}"
            self.last_break = brk
            self.last_break_idx = i

    # ------------------------------------------------------------ §6 trend-line breaks

    def _check_lines(self, i: int, bar: Bar, changes: list[dict[str, Any]]) -> None:
        still: list[TrendLine] = []
        for line in self.lines:
            if i - line.last_touch_idx > self.cfg.trendline_max_idle_bars:
                line.expired_at = bar.close_time
                changes.append(line.item())
                continue
            v = line.value(bar.open_time)
            broken = (
                bar.close < v - line.tol if line.side == "support" else bar.close > v + line.tol
            )
            if broken:
                line.broken_at = bar.close_time
                line.broken_time = bar.open_time
                changes.append(line.item())
            else:
                still.append(line)
        self.lines = still

    # ------------------------------------------------------------ §2-3 swings

    def _check_pivots(self, i: int, bar: Bar, changes: list[dict[str, Any]]) -> None:
        cfg = self.cfg
        p = i - cfg.right_bars
        if self.atr is None or self._bar(p - cfg.left_bars) is None:
            return
        pb = self._bar(p)
        assert pb is not None
        left = [self._bar(j) for j in range(p - cfg.left_bars, p)]
        right = [self._bar(j) for j in range(p + 1, i + 1)]
        is_high = all(pb.high > b.high for b in left if b) and all(
            pb.high >= b.high for b in right if b
        )
        is_low = all(pb.low < b.low for b in left if b) and all(pb.low <= b.low for b in right if b)
        for side, ok in (("high", is_high), ("low", is_low)):
            if ok:
                self._consider_swing(side, p, pb, i, bar, changes)

    def _consider_swing(
        self, side: str, p: int, pb: Bar, i: int, bar: Bar, changes: list[dict[str, Any]]
    ) -> None:
        cfg = self.cfg
        atr = self.atr
        assert atr is not None
        start = (
            self.last_accepted_idx + 1 if self.last_accepted_idx is not None else p - cfg.left_bars
        )
        start = min(max(start, self.n - len(self.bars)), p)
        window = [b for b in (self._bar(j) for j in range(start, p + 1)) if b]
        if side == "high":
            size = pb.high - min(b.low for b in window)
        else:
            size = max(b.high for b in window) - pb.low
        if size < cfg.min_swing_atr * atr:
            return
        price = pb.high if side == "high" else pb.low
        prev = self.last_high if side == "high" else self.last_low
        if prev is None:
            label = None
        elif side == "high":
            label = "HH" if price > prev.price else "LH"
        else:
            label = "LL" if price < prev.price else "HL"
        sw = Swing(
            side=side,  # type: ignore[arg-type]
            price=price,
            pivot_time=pb.open_time,
            confirmed_at=bar.close_time,
            label=label,
            idx=p,
            atr=atr,
        )
        self.swings.append(sw)
        self.last_accepted_idx = p
        if side == "high":
            self.last_high = self.level_high = sw
        else:
            self.last_low = self.level_low = sw
        changes.append(sw.item())
        self._lines_on_swing(sw, i, bar, changes)

    # ------------------------------------------------------------ §6 trend lines

    def _lines_on_swing(self, sw: Swing, i: int, bar: Bar, changes: list[dict[str, Any]]) -> None:
        cfg = self.cfg
        assert self.atr is not None
        side = "support" if sw.side == "low" else "resistance"
        touched = False
        for line in self.lines:
            if line.side == side and abs(line.value(sw.pivot_time) - sw.price) <= line.tol:
                line.touches.append(Touch(sw.pivot_time, sw.price, sw.confirmed_at))
                line.last_touch_idx = sw.idx
                changes.append(line.item())
                touched = True
        if touched:
            return

        tol = cfg.trendline_tolerance_atr * self.atr
        same = [s for s in self.swings if s.side == sw.side and s is not sw]
        prev = same[-cfg.trendline_pivot_window :]
        best: tuple[tuple[int, int], TrendLine] | None = None
        for ai, a in enumerate(prev):
            for b in prev[ai + 1 :]:
                rising = b.price > a.price
                if (side == "support") != rising:
                    continue
                line = TrendLine(
                    side, a.pivot_time, a.price, b.pivot_time, b.price, tol, bar.close_time
                )  # type: ignore[arg-type]
                if line.id in self.line_ids or abs(line.value(sw.pivot_time) - sw.price) > tol:
                    continue
                touches = [
                    s for s in [*prev[ai:], sw] if abs(line.value(s.pivot_time) - s.price) <= tol
                ]
                if len(touches) < cfg.trendline_min_touches:
                    continue
                if not self._no_close_through(line, a.idx, i):
                    continue
                score = (len(touches), -a.pivot_time)
                if best is None or score > best[0]:
                    line.touches = [Touch(s.pivot_time, s.price, s.confirmed_at) for s in touches]
                    line.last_touch_idx = sw.idx
                    best = (score, line)
        if best is None:
            return
        line = best[1]
        self.lines.append(line)
        self.line_ids.add(line.id)
        changes.append(line.item())
        same_side = [ln for ln in self.lines if ln.side == side]
        if len(same_side) > cfg.max_active_trendlines:
            oldest = min(same_side, key=lambda ln: ln.last_touch)
            oldest.expired_at = bar.close_time
            self.lines.remove(oldest)
            changes.append(oldest.item())

    def _no_close_through(self, line: TrendLine, from_idx: int, to_idx: int) -> bool:
        for j in range(from_idx, to_idx + 1):
            b = self._bar(j)
            if b is None:
                return False  # cannot verify: do not create the line
            v = line.value(b.open_time)
            if line.side == "support" and b.close < v - line.tol:
                return False
            if line.side == "resistance" and b.close > v + line.tol:
                return False
        return True

    # ------------------------------------------------------------ §5, §7 state

    def best_line(self) -> TrendLine | None:
        def rank(ln: TrendLine) -> tuple[int, int]:
            return (len(ln.touches), ln.last_touch)

        if self.trend == "bullish":
            pool = [ln for ln in self.lines if ln.side == "support"]
            return max(pool, key=rank, default=None)
        if self.trend == "bearish":
            pool = [ln for ln in self.lines if ln.side == "resistance"]
            return max(pool, key=rank, default=None)
        return max(self.lines, key=lambda ln: ln.last_touch, default=None)

    def state(self) -> dict[str, Any]:
        line = self.best_line()
        return {
            "as_of": self.as_of,
            "trend": self.trend,
            "reason": self.reason,
            "last_swing_high": self.last_high.summary() if self.last_high else None,
            "last_swing_low": self.last_low.summary() if self.last_low else None,
            "last_event": self.last_break.summary() if self.last_break else None,
            "active_trendline": line.summary() if line else None,
            "atr": self.atr,
        }
