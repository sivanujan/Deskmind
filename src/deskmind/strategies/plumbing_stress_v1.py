"""Plumbing Stress V1 strategy.

A test-only strategy used strictly to exercise the execution plumbing.
Refuses to load outside testnet/paper environments.
Every 20-30 minutes opens a minimum-size position on a rotating symbol
with a tight stop and take-profit designed to pass all Risk Manager rules unchanged.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from deskmind.strategies.base import BaseStrategy, Intent, StrategyContext

logger = logging.getLogger(__name__)


class PlumbingStressV1(BaseStrategy):
    name = "plumbing_stress_v1"
    version = "1.0.0"

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        trading_mode: str = "paper",
    ) -> None:
        super().__init__(params)
        self.trading_mode = (self.params.get("trading_mode") or trading_mode).lower()

        # Hard safety requirement: refuses to load outside testnet/paper
        if self.trading_mode not in ("paper", "testnet"):
            raise RuntimeError(
                f"Strategy 'plumbing_stress_v1' is a test-only strategy and "
                f"REFUSES to load in '{self.trading_mode}' mode. Only 'paper' and 'testnet' are allowed."
            )

        self.interval_minutes = int(self.params.get("interval_minutes", 25))
        self.interval_ms = self.interval_minutes * 60 * 1000
        self._last_intent_ms: int = 0
        self._symbol_rotation_idx: int = 0
        self._last_side: str = "short"  # Alternates between long and short

    def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
        if not contexts:
            return []

        # Sort symbols deterministically
        symbols = sorted(contexts.keys())
        first_ctx = contexts[symbols[0]]
        current_ms = first_ctx.as_of_ms

        # Any open position in account snapshot? If so, wait
        for ctx in contexts.values():
            if ctx.account.open_position is not None:
                return []

        # Check time interval
        if self._last_intent_ms > 0 and (current_ms - self._last_intent_ms) < self.interval_ms:
            return []

        # Rotate symbol
        symbol = symbols[self._symbol_rotation_idx % len(symbols)]
        self._symbol_rotation_idx += 1
        ctx = contexts[symbol]

        close_price = ctx.current_bar.close
        if close_price <= Decimal("0"):
            return []

        # Alternate side
        side = "long" if self._last_side == "short" else "short"
        self._last_side = side

        # Get ATR or default to 1.5% of price
        atr = Decimal("0")
        tf_1h = ctx.agent_report.get("timeframes", {}).get("1h", {})
        if tf_1h.get("atr"):
            atr = Decimal(str(tf_1h["atr"]))
        if atr <= Decimal("0"):
            atr = close_price * Decimal("0.015")

        # Stop distance: ensure it satisfies:
        # 1. >= min_stop_atr_mult (0.5 * atr)
        # 2. >= min_stop_price_pct (0.3% of price)
        # 3. friction cost < 15% of risk (stop distance >= 1.2% for ~0.08% fee)
        # 4. <= max_stop_price_pct (8% of price)
        stop_dist = max(atr * Decimal("1.2"), close_price * Decimal("0.015"))
        stop_dist = min(stop_dist, close_price * Decimal("0.05"))

        # Target distance: ensure net reward:risk >= 1.5 -> target_dist = 2.0 * stop_dist
        target_dist = stop_dist * Decimal("2.0")

        if side == "long":
            stop_loss = close_price - stop_dist
            take_profit = close_price + target_dist
            action = "open_long"
        else:
            stop_loss = close_price + stop_dist
            take_profit = close_price - target_dist
            action = "open_short"

        setup_id = f"stress_{symbol}_{current_ms}_{side}"
        self._last_intent_ms = current_ms

        logger.info(
            "PlumbingStressV1 emitted %s for %s at %s (stop: %s, tp: %s, setup: %s)",
            action,
            symbol,
            close_price,
            stop_loss,
            take_profit,
            setup_id,
        )

        return [
            Intent(
                symbol=symbol,
                action=action,
                order_type="market",
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata={
                    "strategy": self.name,
                    "version": self.version,
                    "stress": True,
                },
                setup_id=setup_id,
            )
        ]
