"""Backtest strategy for H6_v1.0.0 (4h Trend Breakout with 10-day Time Exit).

Specification:
- Bars: 4h, resampled from 5m, CLOSED bars only (no look-ahead).
- Long: 4h close > highest high of previous 20 4h bars AND EMA50(4h) rising (EMA50 now > EMA50 5 bars ago).
- Short: 4h close < lowest low of previous 20 4h bars AND EMA50(4h) falling (EMA50 now < EMA50 5 bars ago).
- Entry: market at next 5m bar open after the 4h close.
- Stop: 2.0 × ATR14(4h). Target: 2R. Max hold: 10 days → time exit.
- One signal per breakout (no re-entry until a new 20-bar high/low).
- Same universe, fees, honest TP rule, guards, gates, random baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from deskmind.research.hypotheses.h6_trend_breakout import (
    FOUR_HOURS_MS,
    TEN_DAYS_MS,
    Bar4h,
    H6TrendBreakoutPlugin,
)
from deskmind.strategies.base import BaseStrategy, Intent, StrategyContext


class H6TrendBreakoutStrategy(BaseStrategy):
    """BacktestEngine Strategy adapter for H6_v1.0.0 (4h Trend Breakout)."""

    name = "h6_trend_breakout"
    version = "1.0.0"
    uses_agents = ("trend",)
    timeframes = ("4h", "5m")

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.plugin = H6TrendBreakoutPlugin()

        # Resampling state per symbol:
        # symbol -> list of completed closed 4h bars
        self._closed_4h: dict[str, list[Bar4h]] = {}
        # symbol -> current ongoing 4h bucket aggregator
        self._ongoing_bucket: dict[str, dict[str, Any]] = {}

        # Breakout state machine per symbol:
        # Tracks whether symbol is currently in an active breakout to enforce 'one signal per breakout'
        self._in_long_breakout: dict[str, bool] = {}
        self._in_short_breakout: dict[str, bool] = {}

        # Deduplication of emitted setup IDs
        self._emitted_setup_ids: set[str] = set()

    def _update_resampling(
        self,
        symbol: str,
        b_open: float,
        b_high: float,
        b_low: float,
        b_close: float,
        b_vol: float,
        open_time: int,
        close_time: int,
    ) -> Bar4h | None:
        """Process incoming 5m bar and return a completed 4h Bar if one just closed, else None."""
        bucket_start = (open_time // FOUR_HOURS_MS) * FOUR_HOURS_MS
        bucket_end = bucket_start + FOUR_HOURS_MS

        cur = self._ongoing_bucket.get(symbol)
        if cur is None or cur["start"] != bucket_start:
            # Start new 4h bucket
            cur = {
                "start": bucket_start,
                "end": bucket_end,
                "open": b_open,
                "high": b_high,
                "low": b_low,
                "close": b_close,
                "volume": b_vol,
            }
            self._ongoing_bucket[symbol] = cur
        else:
            # Update current bucket
            cur["high"] = max(cur["high"], b_high)
            cur["low"] = min(cur["low"], b_low)
            cur["close"] = b_close
            cur["volume"] += b_vol

        # Check if this 5m bar closed the 4h candle
        if close_time == bucket_end:
            completed_bar = Bar4h(
                open_time=cur["start"],
                close_time=cur["end"],
                open=cur["open"],
                high=cur["high"],
                low=cur["low"],
                close=cur["close"],
                volume=cur["volume"],
            )
            self._closed_4h.setdefault(symbol, []).append(completed_bar)
            return completed_bar

        return None

    def _log_progress(self, msg: str) -> None:
        log_file = self.params.get("progress_log_file")
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except Exception:
                pass

    def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
        intents: list[Intent] = []

        for symbol, ctx in contexts.items():
            current_bar = ctx.current_bar

            # 1. Check 10-day time exit for active position
            if ctx.account and ctx.account.open_position:
                pos = ctx.account.open_position
                if pos.symbol == symbol:
                    holding_ms = ctx.as_of_ms - pos.entry_time_ms
                    if holding_ms >= TEN_DAYS_MS:
                        sid = f"time_exit_{symbol}_{pos.entry_time_ms}"
                        if sid not in self._emitted_setup_ids:
                            self._emitted_setup_ids.add(sid)
                            self._log_progress(
                                f"{datetime.fromtimestamp(ctx.as_of_ms / 1000.0, tz=UTC).isoformat()} "
                                f"[{symbol}] TIME EXIT emitted (holding_time = {holding_ms / 86_400_000:.1f} days >= 10 days)"
                            )
                            intents.append(
                                Intent(
                                    symbol=symbol,
                                    action="close",
                                    setup_id=sid,
                                    metadata={"exit_reason": "time_limit"},
                                )
                            )

            # 2. Resample 5m bar into 4h bars
            b_open = float(current_bar.open)
            b_high = float(current_bar.high)
            b_low = float(current_bar.low)
            b_close = float(current_bar.close)
            b_vol = float(current_bar.volume)

            new_4h_bar = self._update_resampling(
                symbol,
                b_open,
                b_high,
                b_low,
                b_close,
                b_vol,
                current_bar.open_time,
                current_bar.close_time,
            )

            # Strategy signals are evaluated ONLY at the close of a 4h bar
            if new_4h_bar is None:
                continue

            bars = self._closed_4h[symbol]
            n_bars = len(bars)

            # Need at least 55 closed 4h bars (50 for EMA50 + 5 for slope, and at least 20 for channel)
            if n_bars < 55:
                continue

            # 3. Indicators on strictly CLOSED 4h bars
            # Compute EMA50 on closes
            alpha = 2.0 / 51.0
            closes = [b.close for b in bars]
            ema_val = sum(closes[:50]) / 50.0
            ema_series = [ema_val]
            for c in closes[50:]:
                ema_val = c * alpha + ema_val * (1.0 - alpha)
                ema_series.append(ema_val)

            ema50_now = ema_series[-1]
            ema50_5ago = ema_series[-6]
            ema_rising = ema50_now > ema50_5ago
            ema_falling = ema50_now < ema50_5ago

            # Compute ATR14 on closed bars
            tr_list = []
            for i in range(1, n_bars):
                h = bars[i].high
                l = bars[i].low
                prev_c = bars[i - 1].close
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                tr_list.append(tr)

            atr_val = sum(tr_list[:14]) / 14.0
            for tr in tr_list[14:]:
                atr_val = (atr_val * 13.0 + tr) / 14.0

            # 20-bar channel high/low of previous 20 4h bars (indices -21 to -2 inclusive)
            prev_20_bars = bars[-21:-1]
            channel_high = max(b.high for b in prev_20_bars)
            channel_low = min(b.low for b in prev_20_bars)

            bar_close = new_4h_bar.close
            in_long = self._in_long_breakout.get(symbol, False)
            in_short = self._in_short_breakout.get(symbol, False)

            # 4. Breakout evaluation & One-signal-per-breakout state machine
            # Long rule: close > highest high of previous 20 4h bars AND EMA50 rising
            if bar_close > channel_high:
                if not in_long and ema_rising:
                    self._in_long_breakout[symbol] = True
                    self._in_short_breakout[symbol] = False

                    stop_dist = Decimal(str(round(2.0 * atr_val, 6)))
                    target_dist = stop_dist * Decimal("2.0")  # 2R target

                    sid = f"H6:{symbol}:{new_4h_bar.close_time}:long"
                    if sid not in self._emitted_setup_ids:
                        self._emitted_setup_ids.add(sid)
                        c_dec = Decimal(str(round(bar_close, 6)))
                        stop_val = c_dec - stop_dist
                        target_val = c_dec + target_dist
                        self._log_progress(
                            f"{datetime.fromtimestamp(new_4h_bar.close_time / 1000.0, tz=UTC).isoformat()} "
                            f"[{symbol}] LONG signal @ {bar_close:.4f} (SL={stop_val}, TP={target_val}, ATR={atr_val:.4f})"
                        )

                        intents.append(
                            Intent(
                                symbol=symbol,
                                action="open_long",
                                order_type="market",
                                stop_loss=stop_val,
                                take_profit=target_val,
                                ranking_score=1.0,
                                setup_id=sid,
                                metadata={
                                    "hypothesis_id": "H6_v1.0.0",
                                    "setup_id": sid,
                                    "atr14_4h": float(atr_val),
                                    "channel_high": float(channel_high),
                                    "ema50_rising": True,
                                },
                            )
                        )
            else:
                self._in_long_breakout[symbol] = False

            # Short rule: close < lowest low of previous 20 4h bars AND EMA50 falling
            if bar_close < channel_low:
                if not in_short and ema_falling:
                    self._in_short_breakout[symbol] = True
                    self._in_long_breakout[symbol] = False

                    stop_dist = Decimal(str(round(2.0 * atr_val, 6)))
                    target_dist = stop_dist * Decimal("2.0")  # 2R target

                    sid = f"H6:{symbol}:{new_4h_bar.close_time}:short"
                    if sid not in self._emitted_setup_ids:
                        self._emitted_setup_ids.add(sid)
                        c_dec = Decimal(str(round(bar_close, 6)))
                        stop_val = c_dec + stop_dist
                        target_val = c_dec - target_dist
                        self._log_progress(
                            f"{datetime.fromtimestamp(new_4h_bar.close_time / 1000.0, tz=UTC).isoformat()} "
                            f"[{symbol}] SHORT signal @ {bar_close:.4f} (SL={stop_val}, TP={target_val}, ATR={atr_val:.4f})"
                        )

                        intents.append(
                            Intent(
                                symbol=symbol,
                                action="open_short",
                                order_type="market",
                                stop_loss=stop_val,
                                take_profit=target_val,
                                ranking_score=1.0,
                                setup_id=sid,
                                metadata={
                                    "hypothesis_id": "H6_v1.0.0",
                                    "setup_id": sid,
                                    "atr14_4h": float(atr_val),
                                    "channel_low": float(channel_low),
                                    "ema50_falling": True,
                                },
                            )
                        )
            else:
                self._in_short_breakout[symbol] = False

        return intents
