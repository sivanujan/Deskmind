"""Kill switch for manual and automated emergency liquidation and trading halts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deskmind.logging import get_logger

if TYPE_CHECKING:
    from deskmind.execution.executor import BaseExecutor
    from deskmind.risk.breakers import CircuitBreakerTracker

log = get_logger(__name__)


class KillSwitch:
    """Emergency halt and liquidation mechanism.

    Actions on kill:
    1. Cancel all open orders across all symbols immediately.
    2. Close all open positions via reduce-only market orders.
    3. Transition circuit breaker into 'full_stop' halt state.
    4. Persist kill switch engagement flag to disk.
    """

    def __init__(
        self,
        executor: BaseExecutor,
        tracker: CircuitBreakerTracker | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.executor = executor
        self.tracker = tracker
        self.state_file = state_file or Path("data/journal/kill_switch_state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.is_active = self._load_state()

    def _load_state(self) -> bool:
        if self.state_file.exists():
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    data = json.load(f)
                    return bool(data.get("is_active", False))
            except Exception:
                return False
        return False

    def _save_state(self, reason: str) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"is_active": self.is_active, "reason": reason}, f, indent=2)
        except Exception as e:
            log.error("kill_switch_save_error", error=str(e))

    @property
    def is_halted(self) -> bool:
        return self.is_active

    async def trigger_halt(self, reason: str) -> dict[str, Any]:
        return await self.trigger(reason)

    async def trigger(self, reason: str) -> dict[str, Any]:
        """Engage the kill switch immediately."""
        self.is_active = True
        self._save_state(reason)

        log.critical(
            "KILL_SWITCH_ENGAGED",
            reason=reason,
            msg="Cancelling all orders and flattening all open positions!",
        )

        results: dict[str, Any] = {"reason": reason, "orders_canceled": [], "positions_closed": []}

        # 1. Cancel all open orders
        try:
            canceled = await self.executor.cancel_all()
            results["orders_canceled"] = canceled
            log.info("kill_switch_orders_canceled", count=len(canceled))
        except Exception as e:
            log.error("kill_switch_cancel_orders_failed", error=str(e))

        # 2. Flatten all open positions
        try:
            positions = await self.executor.get_positions()
            for sym, pos in positions.items():
                if pos.is_open:
                    close_res = await self.executor.close(sym, reason=f"kill_switch: {reason}")
                    results["positions_closed"].append(
                        {"symbol": sym, "success": close_res.success, "qty": str(pos.qty)}
                    )
                    log.info("kill_switch_position_closed", symbol=sym, qty=str(pos.qty))
        except Exception as e:
            log.error("kill_switch_close_positions_failed", error=str(e))

        # 3. Halt Circuit Breaker
        if self.tracker is not None:
            self.tracker.halt_state = "full_stop"

        return results

    def reset(self) -> bool:
        """Manually disengage the kill switch."""
        self.is_active = False
        self._save_state("manual_reset")
        log.warning("kill_switch_disengaged")
        return True
