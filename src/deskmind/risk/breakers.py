"""Stateful circuit breaker tracker for drawdown, daily loss, and streak limits."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from deskmind.risk.config import RiskConfig
from deskmind.risk.models import AccountState

logger = logging.getLogger("deskmind.risk.breakers")


@dataclass
class CircuitBreakerTransition:
    """Record of a state transition."""

    timestamp_ms: int
    timestamp_iso: str
    from_state: str
    to_state: str
    reason: str


class CircuitBreakerTracker:
    """Tracks account equity, daily loss, drawdowns, and loss streaks over time."""

    def __init__(
        self,
        config: RiskConfig | None = None,
        initial_equity: Decimal = Decimal("10000.0"),
    ) -> None:
        self.config = config or RiskConfig()
        self.peak_equity = initial_equity
        self.start_of_day_equity = initial_equity
        self.today_realized_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.cooldown_until_ms = 0
        self.halt_state = (
            "normal"  # normal, daily_loss_halt, drawdown_halt, full_stop, streak_cooldown
        )
        self.current_day_utc = ""
        self.transitions: list[CircuitBreakerTransition] = []

    def _log_transition(self, to_state: str, reason: str, timestamp_ms: int) -> None:
        if self.halt_state == to_state:
            return
        from_state = self.halt_state
        self.halt_state = to_state
        dt_iso = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
        t = CircuitBreakerTransition(
            timestamp_ms=timestamp_ms,
            timestamp_iso=dt_iso,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
        self.transitions.append(t)
        logger.warning(
            "Circuit Breaker Transition at %s: [%s] -> [%s] (%s)",
            dt_iso,
            from_state,
            to_state,
            reason,
        )

    def on_time_and_equity(self, timestamp_ms: int, current_equity: Decimal) -> None:
        """Call on every candle close with current time and equity."""
        if timestamp_ms <= 0:
            return

        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        day_str = dt.strftime("%Y-%m-%d")

        # 1. Day boundary reset (UTC 00:00:00)
        if day_str != self.current_day_utc:
            self.current_day_utc = day_str
            self.start_of_day_equity = current_equity
            self.today_realized_pnl = Decimal("0")
            if self.halt_state == "daily_loss_halt":
                self._log_transition(
                    "normal",
                    "New UTC trading day arrived, daily loss halt cleared",
                    timestamp_ms,
                )

        # 2. Update peak equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        # 3. Check cooldown expiry
        if self.cooldown_until_ms > 0 and timestamp_ms >= self.cooldown_until_ms:
            self.cooldown_until_ms = 0
            if self.halt_state == "streak_cooldown":
                self._log_transition(
                    "normal",
                    "24h loss streak cooldown expired",
                    timestamp_ms,
                )

        # 4. Check drawdown limits
        if self.peak_equity > Decimal("0") and current_equity > Decimal("0"):
            drawdown = (self.peak_equity - current_equity) / self.peak_equity
            if drawdown >= self.config.drawdown_full_stop_pct:
                if self.halt_state != "full_stop":
                    self._log_transition(
                        "full_stop",
                        f"Drawdown {drawdown * 100:.1f}% reached 20% full stop threshold",
                        timestamp_ms,
                    )
            elif drawdown >= self.config.drawdown_halt_pct:
                if self.halt_state == "normal":
                    self._log_transition(
                        "drawdown_halt",
                        f"Drawdown {drawdown * 100:.1f}% reached 15% halt threshold",
                        timestamp_ms,
                    )
            elif self.halt_state == "drawdown_halt" and drawdown < self.config.drawdown_halt_pct:
                # Recovered above 15% drawdown
                self._log_transition(
                    "normal",
                    f"Drawdown recovered to {drawdown * 100:.1f}% (< 15%)",
                    timestamp_ms,
                )

    def record_trade_exit(self, pnl: Decimal, exit_time_ms: int) -> None:
        """Call whenever a trade is exited to update streaks and daily PnL."""
        self.today_realized_pnl += pnl

        if pnl < Decimal("0"):
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.config.consecutive_losses_threshold:
                cooldown_dur_ms = self.config.consecutive_losses_cooldown_hours * 3600 * 1000
                self.cooldown_until_ms = exit_time_ms + cooldown_dur_ms
                if self.halt_state == "normal":
                    self._log_transition(
                        "streak_cooldown",
                        f"{self.consecutive_losses} consecutive losses reached, pausing for 24h",
                        exit_time_ms,
                    )
        else:
            self.consecutive_losses = 0

        # Check daily loss limit
        if self.start_of_day_equity > Decimal("0") and self.today_realized_pnl < Decimal("0"):
            limit = self.start_of_day_equity * self.config.daily_loss_limit_pct
            if abs(self.today_realized_pnl) >= limit and self.halt_state == "normal":
                self._log_transition(
                    "daily_loss_halt",
                    f"Daily loss ${abs(self.today_realized_pnl):.2f} reached 3% limit (${limit:.2f})",
                    exit_time_ms,
                )

    def reset_full_stop(self, current_equity: Decimal, timestamp_ms: int) -> bool:
        """Manually reset the full stop circuit breaker after 20% drawdown."""
        if self.halt_state != "full_stop":
            return False

        self.peak_equity = current_equity
        self._log_transition(
            "normal",
            "Manual reset command executed with typed confirmation",
            timestamp_ms,
        )
        return True

    def get_account_state(
        self,
        equity: Decimal,
        available_balance: Decimal,
        open_positions: list[str] | None = None,
    ) -> AccountState:
        """Construct an AccountState snapshot for evaluation."""
        return AccountState(
            equity=equity,
            available_balance=available_balance,
            open_positions=open_positions or [],
            today_realized_pnl=self.today_realized_pnl,
            peak_equity=self.peak_equity,
            consecutive_losses=self.consecutive_losses,
            halt_state=self.halt_state,
            start_of_day_equity=self.start_of_day_equity,
            cooldown_until_ms=self.cooldown_until_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for persistent storage / CLI."""
        return {
            "peak_equity": float(self.peak_equity),
            "start_of_day_equity": float(self.start_of_day_equity),
            "today_realized_pnl": float(self.today_realized_pnl),
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until_ms": self.cooldown_until_ms,
            "halt_state": self.halt_state,
            "current_day_utc": self.current_day_utc,
            "transitions_count": len(self.transitions),
            "transitions": [asdict(t) for t in self.transitions],
        }

    def save_state(self, path: Path) -> None:
        """Save circuit breaker state to JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_state(cls, path: Path, config: RiskConfig | None = None) -> CircuitBreakerTracker:
        """Load circuit breaker state from JSON."""
        tracker = cls(config=config)
        if not path.exists():
            return tracker
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            tracker.peak_equity = Decimal(str(data.get("peak_equity", "10000.0")))
            tracker.start_of_day_equity = Decimal(
                str(data.get("start_of_day_equity", tracker.peak_equity))
            )
            tracker.today_realized_pnl = Decimal(str(data.get("today_realized_pnl", "0")))
            tracker.consecutive_losses = int(data.get("consecutive_losses", 0))
            tracker.cooldown_until_ms = int(data.get("cooldown_until_ms", 0))
            tracker.halt_state = str(data.get("halt_state", "normal"))
            tracker.current_day_utc = str(data.get("current_day_utc", ""))
            return tracker
        except Exception as e:
            logger.error("Failed to load breaker state from %s: %s", path, e)
            return tracker
