"""Trend Follow V1 strategy (used to validate the backtest engine, not to make money).

Rules:
- Long when 1h trend = bullish AND a 15m BOS up occurs.
  Stop: beyond the last confirmed 15m swing low.
  Target: 2R. No trailing.
- Short when 1h trend = bearish AND a 15m BOS down occurs.
  Stop: beyond the last confirmed 15m swing high.
  Target: 2R. No trailing.
- Ranking across symbols: larger 1h ATR-normalized trend strength first.
  For long: (current_close - 1h_last_swing_low) / 1h_atr.
  For short: (1h_last_swing_high - current_close) / 1h_atr.
  This measures the impulse distance from the foundational structure relative to volatility.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from deskmind.strategies.base import BaseStrategy, Intent, StrategyContext


class TrendFollowV1(BaseStrategy):
    name = "trend_follow_v1"
    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.r_multiple = Decimal(str(self.params.get("r_multiple", 2.0)))

    def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
        intents: list[Intent] = []

        for symbol, ctx in contexts.items():
            # If account already has a position in this symbol, check if it needs management
            # (In Phase 1, target and stop are static exchange-side orders, no trailing)
            if ctx.account.open_position is not None:
                continue

            timeframes = ctx.agent_report.get("timeframes", {})
            tf_1h = timeframes.get("1h", {})
            tf_15m = timeframes.get("15m", {})

            trend_1h = tf_1h.get("trend")
            event_15m = tf_15m.get("last_event")

            # Must have a 15m break confirmed at this exact boundary
            confirmed_at = event_15m.get("confirmed_at") if event_15m else None
            if not event_15m or (
                confirmed_at is not None and abs(confirmed_at - ctx.as_of_ms) > 1000
            ):
                continue
            if event_15m.get("type") != "BOS":
                continue

            direction = event_15m.get("direction")
            close_price = ctx.current_bar.close
            atr_1h = float(tf_1h.get("atr") or 0.0)

            # LONG: 1h bullish + 15m BOS up
            if trend_1h == "bullish" and direction == "up":
                swing_low = tf_15m.get("last_swing_low")
                if not swing_low or swing_low.get("price") is None:
                    continue
                sl_price = Decimal(str(swing_low["price"]))
                if sl_price >= close_price:
                    continue  # Invalid stop (at or above current close)

                risk = close_price - sl_price
                tp_price = close_price + (risk * self.r_multiple)

                # Ranking score: 1h ATR-normalized trend strength
                score = 0.0
                sw_1h = tf_1h.get("last_swing_low")
                if sw_1h and sw_1h.get("price") is not None and atr_1h > 0:
                    score = (float(close_price) - float(sw_1h["price"])) / atr_1h

                intents.append(
                    Intent(
                        symbol=symbol,
                        action="open_long",
                        order_type="market",
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        ranking_score=score,
                        metadata={
                            "rule": "1h_bullish+15m_bos_up",
                            "risk": str(risk),
                            "swing_low": str(sl_price),
                            "score": score,
                        },
                    )
                )

            # SHORT: 1h bearish + 15m BOS down
            elif trend_1h == "bearish" and direction == "down":
                swing_high = tf_15m.get("last_swing_high")
                if not swing_high or swing_high.get("price") is None:
                    continue
                sl_price = Decimal(str(swing_high["price"]))
                if sl_price <= close_price:
                    continue  # Invalid stop (at or below current close)

                risk = sl_price - close_price
                tp_price = close_price - (risk * self.r_multiple)

                # Ranking score: 1h ATR-normalized trend strength
                score = 0.0
                sw_1h = tf_1h.get("last_swing_high")
                if sw_1h and sw_1h.get("price") is not None and atr_1h > 0:
                    score = (float(sw_1h["price"]) - float(close_price)) / atr_1h

                intents.append(
                    Intent(
                        symbol=symbol,
                        action="open_short",
                        order_type="market",
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        ranking_score=score,
                        metadata={
                            "rule": "1h_bearish+15m_bos_down",
                            "risk": str(risk),
                            "swing_high": str(sl_price),
                            "score": score,
                        },
                    )
                )

        return intents
