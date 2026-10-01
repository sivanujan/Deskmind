"""Incremental ICT/SMC liquidity and PD engine for one (symbol, timeframe).

Builds on the Trend agent's confirmed swings, BOS/CHoCH, and sweeps.
Feed closed candles strictly in time order with `on_candle`. Every item records
`confirmed_at` = the close time of the candle at which it became knowable.
No look-ahead. See docs/agents/ict_smc.md.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from deskmind.agents.ict_smc.config import DEFAULT_CONFIG, IctSmcConfig
from deskmind.agents.ict_smc.models import (
    FVG,
    BreakerBlock,
    Displacement,
    LiquidityPool,
    LiquidityVoid,
    OrderBlock,
    PDRange,
    SMTDivergence,
)
from deskmind.agents.trend.config import DEFAULT_CONFIG as DEFAULT_TREND_CONFIG
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.models import Bar


class IctSmcEngine:
    def __init__(
        self,
        symbol: str,
        timeframe: str,
        cfg: IctSmcConfig = DEFAULT_CONFIG,
        trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
    ) -> None:
        from dataclasses import replace
        self.symbol = symbol
        self.timeframe = timeframe
        self.cfg = cfg
        if trend_cfg.min_warmup_bars != cfg.min_warmup_bars:
            trend_cfg = replace(trend_cfg, min_warmup_bars=cfg.min_warmup_bars)
        self.trend_cfg = trend_cfg
        self.trend_engine = TrendEngine(symbol, timeframe, trend_cfg)

        self.bars: deque[Bar] = deque(maxlen=cfg.bar_memory)
        self.displacements: deque[Displacement] = deque(maxlen=50)
        self.pools: list[LiquidityPool] = []
        self.active_pools: list[LiquidityPool] = []
        self.fvgs: list[FVG] = []
        self.active_fvgs: list[FVG] = []
        self.order_blocks: list[OrderBlock] = []
        self.active_order_blocks: list[OrderBlock] = []
        self._ob_ids: set[str] = set()
        self.breakers: list[BreakerBlock] = []
        self.active_breakers: list[BreakerBlock] = []
        self.voids: list[LiquidityVoid] = []
        self.pd_range: PDRange | None = None
        self.last_sweep: dict[str, Any] | None = None
        self.last_smt: SMTDivergence | None = None
        self.as_of: int | None = None

        # Track recent confirmed swings from trend engine for equal highs/lows
        self.confirmed_swings: deque[dict[str, Any]] = deque(maxlen=100)

    # ------------------------------------------------------------ helpers

    @property
    def atr(self) -> float | None:
        return self.trend_engine.atr

    def _bar(self, idx: int) -> Bar | None:
        if 0 <= idx < len(self.bars):
            return self.bars[idx]
        return None

    # ------------------------------------------------------------ main entry

    def on_candle(
        self, bar: Bar, trend_changes: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Process one CLOSED candle. Returns new/updated items (full item dicts)."""
        if self.bars and bar.open_time <= self.bars[-1].open_time:
            return []  # already processed

        self.bars.append(bar)
        if len(self.bars) < self.cfg.min_warmup_bars:
            return []

        # 1. Step the Trend Engine (or consume external trend changes)
        if trend_changes is None:
            trend_changes = self.trend_engine.on_candle(bar)

        atr = self.trend_engine.atr
        changes: list[dict[str, Any]] = []

        if atr is None:
            self.as_of = bar.close_time
            return changes

        # 2. Check Displacement on the current candle
        disp = self._check_displacement(bar, atr)
        if disp is not None:
            self.displacements.append(disp)
            changes.append(disp.item())

        # 3. Check and update FVGs
        self._check_fvgs(bar, atr, changes)

        # 4. Ingest trend swings -> Form Liquidity Pools (EQH / EQL / Single)
        sweeps_at_bar = [c for c in trend_changes if c.get("kind") == "sweep"]
        if sweeps_at_bar:
            self.last_sweep = sweeps_at_bar[-1]

        self._process_trend_swings(trend_changes, atr, bar, changes)

        # 5. Check active Liquidity Pools against current candle
        self._update_pools(bar, sweeps_at_bar, changes)

        # 6. Check BOS -> Qualify Order Blocks & Update PD Range
        bos_breaks = [
            c for c in trend_changes if c.get("kind") in ("bos", "choch") and c.get("type") == "BOS"
        ]
        for bos in bos_breaks:
            self._on_bos(bos, bar, atr, changes)

        # 7. Update Order Blocks (tests, mitigation, invalidation -> Breaker/Mitigation block)
        self._update_order_blocks(bar, changes)

        # 8. Update Breakers
        self._update_breakers(bar, changes)

        # 9. Check Liquidity Voids
        self._check_liquidity_void(bar, atr, changes)

        # 10. Update PD Range position
        self._update_pd_range_position(bar)

        # 11. Housekeeping / expirations
        self._housekeeping(bar, changes)

        self.as_of = bar.close_time
        return changes

    # ------------------------------------------------------------ §3 displacement

    def _check_displacement(self, bar: Bar, atr: float) -> Displacement | None:
        size = bar.high - bar.low
        if size < self.cfg.displacement_min_atr * atr:
            return None
        body = abs(bar.close - bar.open)
        if size == 0 or body / size < self.cfg.displacement_body_ratio:
            return None
        direction = "bullish" if bar.close > bar.open else "bearish"
        return Displacement(
            direction=direction,
            time=bar.open_time,
            confirmed_at=bar.close_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            size=size,
            atr=atr,
            body_ratio=round(body / size, 4),
        )

    # ------------------------------------------------------------ §4 FVGs

    def _check_fvgs(self, bar: Bar, atr: float, changes: list[dict[str, Any]]) -> None:
        # Detect new FVG from the last 3 candles: c1, c2, c3 (= bar)
        if len(self.bars) >= 3:
            c1 = self.bars[-3]
            c2 = self.bars[-2]
            c3 = self.bars[-1]

            # Tags only (detection is unchanged): size in ATRs and whether the middle candle
            # was a displacement candle. Strategy research decides filters.
            backed = any(d.time == c2.open_time for d in self.displacements)

            # Bullish FVG: c1.high < c3.low
            if c1.high < c3.low:
                gap = c3.low - c1.high
                if gap >= self.cfg.fvg_min_atr * atr:
                    fvg = FVG(
                        direction="bullish",
                        top=c3.low,
                        bottom=c1.high,
                        ce=(c3.low + c1.high) / 2.0,
                        created_time=c2.open_time,
                        confirmed_at=c3.close_time,
                        atr=atr,
                        size_atr=gap / atr,
                        displacement_backed=backed,
                    )
                    self.fvgs.append(fvg)
                    self.active_fvgs.append(fvg)
                    changes.append(fvg.item())

            # Bearish FVG: c1.low > c3.high
            elif c1.low > c3.high:
                gap = c1.low - c3.high
                if gap >= self.cfg.fvg_min_atr * atr:
                    fvg = FVG(
                        direction="bearish",
                        top=c1.low,
                        bottom=c3.high,
                        ce=(c1.low + c3.high) / 2.0,
                        created_time=c2.open_time,
                        confirmed_at=c3.close_time,
                        atr=atr,
                        size_atr=gap / atr,
                        displacement_backed=backed,
                    )
                    self.fvgs.append(fvg)
                    self.active_fvgs.append(fvg)
                    changes.append(fvg.item())

        # Update existing active FVGs
        for fvg in self.active_fvgs:
            if fvg.confirmed_at >= bar.close_time:
                continue  # newly created on this candle
            if fvg.state in ("filled", "inversion") or fvg.expired_at is not None:
                continue

            if fvg.direction == "bullish":
                if bar.low < fvg.top:
                    if bar.close < fvg.bottom:
                        fvg.state = "inversion"
                        fvg.inverted_at = bar.close_time
                        changes.append(fvg.item())
                    elif bar.low <= fvg.bottom:
                        fvg.state = "filled"
                        fvg.filled_at = bar.close_time
                        changes.append(fvg.item())
                    elif fvg.state == "fresh":
                        fvg.state = "partially_filled"
                        fvg.partially_filled_at = bar.close_time
                        changes.append(fvg.item())
            else:  # bearish FVG
                if bar.high > fvg.bottom:
                    if bar.close > fvg.top:
                        fvg.state = "inversion"
                        fvg.inverted_at = bar.close_time
                        changes.append(fvg.item())
                    elif bar.high >= fvg.top:
                        fvg.state = "filled"
                        fvg.filled_at = bar.close_time
                        changes.append(fvg.item())
                    elif fvg.state == "fresh":
                        fvg.state = "partially_filled"
                        fvg.partially_filled_at = bar.close_time
                        changes.append(fvg.item())

        self.active_fvgs = [
            f for f in self.active_fvgs
            if f.state not in ("filled", "inversion") and f.expired_at is None
        ]

    # ------------------------------------------------------------ §2 Liquidity Pools

    def _process_trend_swings(
        self,
        trend_changes: list[dict[str, Any]],
        atr: float,
        bar: Bar,
        changes: list[dict[str, Any]],
    ) -> None:
        swing_changes = [c for c in trend_changes if c.get("kind") in ("swing_high", "swing_low")]
        for sw in swing_changes:
            # The trend engine re-sends a swing (same id) when its state changes: update the
            # stored swing in place; only a NEW swing can form a pool.
            known = next(
                (i for i, s in enumerate(self.confirmed_swings) if s["id"] == sw["id"]), None
            )
            if known is not None:
                self.confirmed_swings[known] = sw
                continue
            self.confirmed_swings.append(sw)
            side = "high" if sw["kind"] == "swing_high" else "low"
            price = sw["price"]

            # Lookback for equal swings: other swings on a DIFFERENT candle only
            lookback = list(self.confirmed_swings)[-self.cfg.pool_swing_lookback : -1]
            eq_swings = [
                s
                for s in lookback
                if s["kind"] == sw["kind"]
                and s["pivot_time"] != sw["pivot_time"]
                and abs(s["price"] - price) <= self.cfg.eq_tolerance_atr * atr
            ]

            if eq_swings:
                # Merge or form Equal Highs / Lows pool
                other = eq_swings[-1]
                level = max(other["price"], price) if side == "high" else min(other["price"], price)
                pool = LiquidityPool(
                    side=side,
                    pool_type="eqh" if side == "high" else "eql",
                    level=level,
                    swings=[other["pivot_time"], sw["pivot_time"]],
                    created_time=other["pivot_time"],
                    confirmed_at=bar.close_time,
                    atr=atr,
                )
            else:
                # Single significant swing pool
                pool = LiquidityPool(
                    side=side,
                    pool_type="single_high" if side == "high" else "single_low",
                    level=price,
                    swings=[sw["pivot_time"]],
                    created_time=sw["pivot_time"],
                    confirmed_at=bar.close_time,
                    atr=atr,
                )

            self.pools.append(pool)
            self.active_pools.append(pool)
            changes.append(pool.item())

    def _update_pools(
        self, bar: Bar, sweeps_at_bar: list[dict[str, Any]], changes: list[dict[str, Any]]
    ) -> None:
        for pool in self.active_pools:
            if (
                pool.confirmed_at >= bar.close_time
                or pool.state == "taken"
                or pool.expired_at is not None
            ):
                continue

            if pool.side == "high":
                if bar.high > pool.level:
                    if bar.close > pool.level:
                        pool.state = "taken"
                        pool.taken_at = bar.close_time
                        changes.append(pool.item())
                    else:
                        if pool.state != "swept":
                            pool.state = "swept"
                            pool.swept_at = bar.close_time
                            for s in sweeps_at_bar:
                                if s.get("direction") == "up":
                                    pool.swept_by = s.get("id")
                            changes.append(pool.item())
            else:  # low (SSL)
                if bar.low < pool.level:
                    if bar.close < pool.level:
                        pool.state = "taken"
                        pool.taken_at = bar.close_time
                        changes.append(pool.item())
                    else:
                        if pool.state != "swept":
                            pool.state = "swept"
                            pool.swept_at = bar.close_time
                            for s in sweeps_at_bar:
                                if s.get("direction") == "down":
                                    pool.swept_by = s.get("id")
                            changes.append(pool.item())

        self.active_pools = [
            p for p in self.active_pools
            if p.state != "taken" and p.expired_at is None
        ]

    # ------------------------------------------------------------ §5 Order Blocks & PD Range

    def _on_bos(
        self, bos: dict[str, Any], bar: Bar, atr: float, changes: list[dict[str, Any]]
    ) -> None:
        direction = bos["direction"]  # "up" or "down"
        bos_open_time = bos["pivot_time"]

        # Qualify Order Block: last opposite candle(s) before displacement move
        # Walk backward from candle before the BOS candle
        bars_list = list(self.bars)
        bos_idx = next(
            (idx for idx, b in enumerate(bars_list) if b.open_time == bos_open_time),
            len(bars_list) - 1,
        )

        ob_candles: list[Bar] = []
        is_target_color = (
            (lambda b: b.close < b.open) if direction == "up" else (lambda b: b.close > b.open)
        )

        # Look back from bos_idx - 1
        for idx in range(bos_idx - 1, max(-1, bos_idx - 12), -1):
            b = bars_list[idx]
            if is_target_color(b):
                ob_candles.append(b)
                if len(ob_candles) >= self.cfg.ob_max_candles:
                    break
            elif ob_candles:
                break  # stopped consecutive sequence

        if ob_candles:
            top = max(b.high for b in ob_candles)
            bottom = min(b.low for b in ob_candles)
            ob = OrderBlock(
                direction="bullish" if direction == "up" else "bearish",
                top=top,
                bottom=bottom,
                mt=(top + bottom) / 2.0,
                created_time=ob_candles[-1].open_time,  # earliest candle in sequence
                confirmed_at=bar.close_time,
                from_bos_id=bos["id"],
                atr=atr,
            )
            # an identical OB (same candles, same bar) from a second BOS change is not a new OB
            if ob.id not in self._ob_ids:
                self._ob_ids.add(ob.id)
                self.order_blocks.append(ob)
                self.active_order_blocks.append(ob)
                changes.append(ob.item())

        # Update Dealing Range / PD Range
        if direction == "up":
            # Origin swing low that produced the move
            swing_lows = [s for s in self.confirmed_swings if s["kind"] == "swing_low"]
            origin_low = swing_lows[-1]["price"] if swing_lows else bar.low
            high = bar.high
            eq = (origin_low + high) / 2.0
            pos = (
                ((bar.close - origin_low) / (high - origin_low) * 100)
                if high > origin_low
                else 50.0
            )
            ote_l = high - self.cfg.ote_high_pct * (high - origin_low)
            ote_h = high - self.cfg.ote_low_pct * (high - origin_low)
            self.pd_range = PDRange(
                low=origin_low,
                high=high,
                from_bos_at=bar.close_time,
                equilibrium=eq,
                zone="discount"
                if bar.close < eq
                else "premium"
                if bar.close > eq
                else "equilibrium",
                position_pct=pos,
                in_ote=ote_l <= bar.close <= ote_h,
                ote_low=ote_l,
                ote_high=ote_h,
                confirmed_at=bar.close_time,
            )
        else:  # direction == "down"
            swing_highs = [s for s in self.confirmed_swings if s["kind"] == "swing_high"]
            origin_high = swing_highs[-1]["price"] if swing_highs else bar.high
            low = bar.low
            eq = (origin_high + low) / 2.0
            pos = ((bar.close - low) / (origin_high - low) * 100) if origin_high > low else 50.0
            ote_l = low + self.cfg.ote_low_pct * (origin_high - low)
            ote_h = low + self.cfg.ote_high_pct * (origin_high - low)
            self.pd_range = PDRange(
                low=low,
                high=origin_high,
                from_bos_at=bar.close_time,
                equilibrium=eq,
                zone="discount"
                if bar.close < eq
                else "premium"
                if bar.close > eq
                else "equilibrium",
                position_pct=pos,
                in_ote=ote_l <= bar.close <= ote_h,
                ote_low=ote_l,
                ote_high=ote_h,
                confirmed_at=bar.close_time,
            )

    def _update_order_blocks(self, bar: Bar, changes: list[dict[str, Any]]) -> None:
        for ob in self.active_order_blocks:
            if (
                ob.confirmed_at >= bar.close_time
                or ob.state == "invalidated"
                or ob.expired_at is not None
            ):
                continue

            if ob.direction == "bullish":
                if bar.low <= ob.top:
                    if bar.close < ob.bottom:
                        ob.state = "invalidated"
                        ob.invalidated_at = bar.close_time
                        changes.append(ob.item())

                        # Flip to Breaker or Mitigation block
                        has_prior_sweep = any(
                            p.state == "swept"
                            and p.swept_at is not None
                            and ob.created_time <= p.swept_at <= bar.close_time
                            for p in self.active_pools
                            if p.side == "high"
                        )
                        breaker = BreakerBlock(
                            direction="bearish",
                            is_mitigation=not has_prior_sweep,
                            top=ob.top,
                            bottom=ob.bottom,
                            created_time=ob.created_time,
                            confirmed_at=bar.close_time,
                            source_ob_id=ob.id,
                            sweep_id=self.last_sweep["id"]
                            if has_prior_sweep and self.last_sweep
                            else None,
                            atr=ob.atr or self.atr,
                        )
                        self.breakers.append(breaker)
                        self.active_breakers.append(breaker)
                        changes.append(breaker.item())
                    elif bar.low <= ob.mt:
                        if ob.state != "mitigated":
                            ob.state = "mitigated"
                            ob.mitigated_at = bar.close_time
                            changes.append(ob.item())
                    elif ob.state == "fresh":
                        ob.state = "tested"
                        ob.tested_at = bar.close_time
                        changes.append(ob.item())
            else:  # bearish OB
                if bar.high >= ob.bottom:
                    if bar.close > ob.top:
                        ob.state = "invalidated"
                        ob.invalidated_at = bar.close_time
                        changes.append(ob.item())

                        has_prior_sweep = any(
                            p.state == "swept"
                            and p.swept_at is not None
                            and ob.created_time <= p.swept_at <= bar.close_time
                            for p in self.active_pools
                            if p.side == "low"
                        )
                        breaker = BreakerBlock(
                            direction="bullish",
                            is_mitigation=not has_prior_sweep,
                            top=ob.top,
                            bottom=ob.bottom,
                            created_time=ob.created_time,
                            confirmed_at=bar.close_time,
                            source_ob_id=ob.id,
                            sweep_id=self.last_sweep["id"]
                            if has_prior_sweep and self.last_sweep
                            else None,
                            atr=ob.atr or self.atr,
                        )
                        self.breakers.append(breaker)
                        self.active_breakers.append(breaker)
                        changes.append(breaker.item())
                    elif bar.high >= ob.mt:
                        if ob.state != "mitigated":
                            ob.state = "mitigated"
                            ob.mitigated_at = bar.close_time
                            changes.append(ob.item())
                    elif ob.state == "fresh":
                        ob.state = "tested"
                        ob.tested_at = bar.close_time
                        changes.append(ob.item())

        self.active_order_blocks = [
            ob for ob in self.active_order_blocks
            if ob.state != "invalidated" and ob.expired_at is None
        ]

    # ------------------------------------------------------------ Breakers

    def _update_breakers(self, bar: Bar, changes: list[dict[str, Any]]) -> None:
        for b in self.active_breakers:
            if (
                b.confirmed_at >= bar.close_time
                or b.state == "invalidated"
                or b.expired_at is not None
            ):
                continue
            if b.direction == "bullish":
                if bar.low < b.bottom:
                    if bar.close < b.bottom:
                        b.state = "invalidated"
                        b.invalidated_at = bar.close_time
                        changes.append(b.item())
                    elif b.state == "active":
                        b.state = "mitigated"
                        b.mitigated_at = bar.close_time
                        changes.append(b.item())
            else:  # bearish breaker
                if bar.high > b.top:
                    if bar.close > b.top:
                        b.state = "invalidated"
                        b.invalidated_at = bar.close_time
                        changes.append(b.item())
                    elif b.state == "active":
                        b.state = "mitigated"
                        b.mitigated_at = bar.close_time
                        changes.append(b.item())

        self.active_breakers = [
            b for b in self.active_breakers
            if b.state != "invalidated" and b.expired_at is None
        ]

    # ------------------------------------------------------------ §6 Liquidity Voids

    def _check_liquidity_void(self, bar: Bar, atr: float, changes: list[dict[str, Any]]) -> None:
        # Check if the last 3-5 candles form a void (large single-direction run).
        if len(self.bars) < 3:
            return
        for run_len in range(3, min(len(self.bars), self.cfg.void_max_candles) + 1):
            candles = list(self.bars)[-run_len:]
            start_b = candles[0]
            end_b = candles[-1]
            net_move = end_b.close - start_b.open
            total_range = max(c.high for c in candles) - min(c.low for c in candles)
            if total_range == 0:
                continue

            if (
                abs(net_move) >= self.cfg.void_min_atr * atr
                and (abs(net_move) / total_range) >= 0.75
            ):
                direction = "bullish" if net_move > 0 else "bearish"
                # Form void if current candle is pause/reversal
                is_reversal = (
                    (bar.close < bar.open) if direction == "bullish" else (bar.close > bar.open)
                )
                if is_reversal:
                    top = max(c.high for c in candles[:-1])
                    bottom = min(c.low for c in candles[:-1])
                    void = LiquidityVoid(
                        direction=direction,
                        top=top,
                        bottom=bottom,
                        created_time=start_b.open_time,
                        confirmed_at=bar.close_time,
                        atr=atr,
                    )
                    # Deduplicate with existing open voids
                    if not any(v.created_time == void.created_time for v in self.voids):
                        self.voids.append(void)
                        changes.append(void.item())
                break

        # Update open voids
        for v in self.voids:
            if v.confirmed_at >= bar.close_time or v.state == "filled" or v.expired_at is not None:
                continue
            if (v.direction == "bullish" and bar.low <= v.bottom) or (
                v.direction == "bearish" and bar.high >= v.top
            ):
                v.state = "filled"
                v.filled_at = bar.close_time
                changes.append(v.item())
            elif v.state == "open" and (
                (v.direction == "bullish" and bar.low < v.top)
                or (v.direction == "bearish" and bar.high > v.bottom)
            ):
                v.state = "rebalancing"
                v.rebalanced_at = bar.close_time
                changes.append(v.item())

    # ------------------------------------------------------------ PD Range position update

    def _update_pd_range_position(self, bar: Bar) -> None:
        if self.pd_range is None:
            return
        low = self.pd_range.low
        high = self.pd_range.high
        if high <= low:
            return
        eq = (low + high) / 2.0
        pos = (bar.close - low) / (high - low) * 100
        self.pd_range.position_pct = pos
        self.pd_range.zone = (
            "discount" if bar.close < eq else "premium" if bar.close > eq else "equilibrium"
        )
        self.pd_range.in_ote = self.pd_range.ote_low <= bar.close <= self.pd_range.ote_high

    # ------------------------------------------------------------ Housekeeping

    def _housekeeping(self, bar: Bar, changes: list[dict[str, Any]]) -> None:
        cfg = self.cfg
        # Expire older active items beyond zone_max_age_bars
        current_time = bar.close_time
        for pool in self.active_pools:
            if (
                pool.state == "active"
                and pool.expired_at is None
                and len(self.bars) >= cfg.zone_max_age_bars
            ):
                first_kept = self.bars[0].open_time
                if pool.created_time < first_kept:
                    pool.expired_at = current_time
                    changes.append(pool.item())

        for fvg in self.active_fvgs:
            if (
                fvg.state in ("fresh", "partially_filled")
                and fvg.expired_at is None
                and len(self.bars) >= cfg.zone_max_age_bars
            ):
                first_kept = self.bars[0].open_time
                if fvg.created_time < first_kept:
                    fvg.expired_at = current_time
                    changes.append(fvg.item())

        for ob in self.active_order_blocks:
            if (
                ob.state in ("fresh", "tested")
                and ob.expired_at is None
                and len(self.bars) >= cfg.zone_max_age_bars
            ):
                first_kept = self.bars[0].open_time
                if ob.created_time < first_kept:
                    ob.expired_at = current_time
                    changes.append(ob.item())

        self.active_pools = [p for p in self.active_pools if p.expired_at is None and p.state != "taken"]
        self.active_fvgs = [f for f in self.active_fvgs if f.expired_at is None and f.state not in ("filled", "inversion")]
        self.active_order_blocks = [o for o in self.active_order_blocks if o.expired_at is None and o.state != "invalidated"]

    # ------------------------------------------------------------ State report

    def state(self) -> dict[str, Any]:
        """Current state for the timeframe, formatted for the agent report."""
        def _last_n(items: list[Any], pred: Any, n: int) -> list[Any]:
            out: list[Any] = []
            for item in reversed(items):
                if pred(item):
                    out.append(item)
                    if len(out) == n:
                        break
            out.reverse()
            return out

        bsl_pools = [
            p.summary()
            for p in _last_n(self.pools, lambda p: p.side == "high", self.cfg.max_active_pools)
        ]
        ssl_pools = [
            p.summary()
            for p in _last_n(self.pools, lambda p: p.side == "low", self.cfg.max_active_pools)
        ]
        active_fvgs_objs = _last_n(self.fvgs, lambda f: f.state != "filled", self.cfg.max_active_fvgs)
        active_obs_objs = _last_n(self.order_blocks, lambda ob: ob.state != "invalidated", self.cfg.max_active_obs)
        active_fvgs = [f.summary() for f in active_fvgs_objs]
        active_obs = [ob.summary() for ob in active_obs_objs]
        active_breakers = [
            b.summary()
            for b in _last_n(self.breakers, lambda b: b.state != "invalidated", self.cfg.max_active_breakers)
        ]

        # PD arrays inside the actionable zone
        pd_arrays_in_zone: list[dict[str, Any]] = []
        if self.pd_range:
            zone = self.pd_range.zone
            eq = self.pd_range.equilibrium
            # Discount zone -> bullish PD arrays (bullish OBs, bullish FVGs)
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
            # Premium zone -> bearish PD arrays (bearish OBs, bearish FVGs)
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
