"""Fault injection hooks for testnet and paper execution resilience testing."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deskmind.config import get_settings
from deskmind.logging import get_logger

if TYPE_CHECKING:
    from deskmind.execution.user_stream import UserStreamManager

log = get_logger(__name__)


@dataclass
class InjectedFaultRecord:
    timestamp_ms: int
    fault_name: str
    details: str
    outcome: str = "pending"


class FaultInjector:
    """Manages controllable fault injections for resilience and recovery verification.

    Strictly disabled and refuses to run outside paper or testnet modes.
    """

    def __init__(
        self,
        trading_mode: str | None = None,
        log_path: Path | None = None,
    ) -> None:
        mode = trading_mode or get_settings().trading_mode
        self.trading_mode = mode.lower()
        if self.trading_mode not in ("paper", "testnet"):
            raise RuntimeError(
                f"SECURITY VIOLATION: Fault injection is STRICTLY FORBIDDEN in TRADING_MODE={self.trading_mode}"
            )
        self.log_path = log_path or Path("data/journal/injected_faults.json")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[InjectedFaultRecord] = self._load_records()

        # In-memory flags
        self.stop_placement_fail_count = 0
        self.api_5xx_count = 0
        self._active_faults: set[str] = set()

    def _load_records(self) -> list[InjectedFaultRecord]:
        if self.log_path.exists():
            try:
                with open(self.log_path, encoding="utf-8") as f:
                    data = json.load(f)
                    return [InjectedFaultRecord(**item) for item in data]
            except Exception:
                return []
        return []

    def _save_records(self) -> None:
        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                json.dump([asdict(r) for r in self.records], f, indent=2)
        except Exception as e:
            log.error("fault_log_save_error", error=str(e))

    def record_outcome(self, fault_name: str, outcome: str) -> None:
        for r in reversed(self.records):
            if r.fault_name == fault_name and r.outcome == "pending":
                r.outcome = outcome
                self._save_records()
                break

    def is_fault_active(self, fault_name: str) -> bool:
        return fault_name in self._active_faults

    def set_fault(self, fault_name: str, enabled: bool = True) -> None:
        if enabled:
            self._active_faults.add(fault_name)
            if fault_name == "stop_failure":
                self.inject_stop_placement_failure(1)
            elif fault_name == "api_5xx":
                self.inject_api_5xx_burst(3)
            elif fault_name == "ws_disconnect":
                self._record_fault("ws_disconnect", "Triggered simulated WS disconnect")
            elif fault_name == "stop_keepalive":
                self._record_fault("stop_keepalive", "Triggered simulated keepalive suppression")
        else:
            self._active_faults.discard(fault_name)
            if fault_name == "stop_failure":
                self.stop_placement_fail_count = 0
            elif fault_name == "api_5xx":
                self.api_5xx_count = 0

    def _record_fault(self, name: str, details: str) -> None:
        now_ms = int(time.time() * 1000)
        rec = InjectedFaultRecord(timestamp_ms=now_ms, fault_name=name, details=details)
        self.records.append(rec)
        self._save_records()

    async def inject_ws_disconnect(self, stream_manager: UserStreamManager | None) -> None:
        """Inject fault: forcefully terminate the active WebSocket connection."""
        self.set_fault("ws_disconnect", True)
        log.warning("fault_injected", fault="force_ws_disconnect")
        if stream_manager is not None:
            await stream_manager.force_disconnect_fault()
            self.record_outcome(
                "ws_disconnect", "WS connection forcibly closed; auto-reconnect triggered"
            )

    def inject_stop_keepalive(self, stream_manager: UserStreamManager | None) -> None:
        """Inject fault: suppress listenKey keepalive pings."""
        self.set_fault("stop_keepalive", True)
        log.warning("fault_injected", fault="stop_user_stream_keepalive")
        if stream_manager is not None:
            stream_manager.stop_keepalive_fault()
            self.record_outcome(
                "stop_keepalive", "Keepalive suppressed; stream will expire after 60m"
            )

    def inject_stop_placement_failure(self, count: int = 1) -> None:
        """Inject fault: cause next N stop-loss order placements to fail."""
        now_ms = int(time.time() * 1000)
        self.stop_placement_fail_count = count
        self._active_faults.add("stop_failure")
        rec = InjectedFaultRecord(
            timestamp_ms=now_ms,
            fault_name="inject_stop_placement_failure",
            details=f"Forced next {count} stop-loss placements to simulate rejection",
        )
        self.records.append(rec)
        self._save_records()
        log.warning("fault_injected", fault="inject_stop_placement_failure", count=count)

    def inject_api_5xx_burst(self, count: int = 3) -> None:
        """Inject fault: simulate next N REST requests returning HTTP 500."""
        now_ms = int(time.time() * 1000)
        self.api_5xx_count = count
        self._active_faults.add("api_5xx")
        rec = InjectedFaultRecord(
            timestamp_ms=now_ms,
            fault_name="inject_api_5xx_burst",
            details=f"Simulating next {count} REST calls returning HTTP 500",
        )
        self.records.append(rec)
        self._save_records()
        log.warning("fault_injected", fault="inject_api_5xx_burst", count=count)

    def check_stop_placement_fault(self) -> bool:
        """Check and consume stop placement failure fault."""
        if self.stop_placement_fail_count > 0:
            self.stop_placement_fail_count -= 1
            if self.stop_placement_fail_count == 0:
                self._active_faults.discard("stop_failure")
            log.warning("fault_triggered: stop placement failure consumed")
            self.record_outcome(
                "inject_stop_placement_failure", "Stop placement failed as injected"
            )
            return True
        return False

    def check_api_5xx_fault(self) -> bool:
        """Check and consume API 5xx failure fault."""
        if self.api_5xx_count > 0:
            self.api_5xx_count -= 1
            if self.api_5xx_count == 0:
                self._active_faults.discard("api_5xx")
            log.warning("fault_triggered: API 5xx error consumed")
            self.record_outcome("inject_api_5xx_burst", "HTTP 500 error triggered")
            return True
        return False

    def get_fault_history(self) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(r.timestamp_ms / 1000)
                ),
                "fault": r.fault_name,
                "details": r.details,
                "status": r.outcome,
            }
            for r in self.records
        ]


_GLOBAL_INJECTOR: FaultInjector | None = None


def get_fault_injector(trading_mode: str | None = None) -> FaultInjector:
    global _GLOBAL_INJECTOR
    if _GLOBAL_INJECTOR is None:
        _GLOBAL_INJECTOR = FaultInjector(trading_mode=trading_mode)
    return _GLOBAL_INJECTOR
