"""Process supervisor for managing live data, trend agent, trade runner and universe services."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

from deskmind.logging import get_logger

log = get_logger(__name__)

ServiceStatus = Literal["stopped", "running", "restarting", "crashed", "stopping"]

SERVICE_COMMANDS: dict[str, list[str]] = {
    "live": [sys.executable, "-m", "deskmind.cli", "live", "run"],
    "trend": [sys.executable, "-m", "deskmind.cli", "agents", "run", "trend"],
    "trade": [sys.executable, "-m", "deskmind.cli", "trade", "run"],
    # Daily universe job at 00:00 UTC (recompute, download/verify/warm up entries).
    "universe": [sys.executable, "-m", "deskmind.cli", "universe", "run"],
}


@dataclass
class ServiceState:
    name: str
    command: list[str]
    status: ServiceStatus = "stopped"
    pid: int | None = None
    started_at: float | None = None
    uptime_s: float = 0.0
    restart_count: int = 0
    last_exit_code: int | None = None
    backoff_delay: float = 1.0
    enabled: bool = False
    process: asyncio.subprocess.Process | None = None


class SupervisorManager:
    """Manages child service processes with automated health monitoring and backoff restarts."""

    def __init__(self, commands: dict[str, list[str]] | None = None) -> None:
        self.commands = commands or dict(SERVICE_COMMANDS)
        self.services: dict[str, ServiceState] = {
            name: ServiceState(name=name, command=cmd) for name, cmd in self.commands.items()
        }
        self._monitor_task: asyncio.Task | None = None
        self._running = False
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Start supervisor monitoring loop."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        log.info("supervisor_started", services=list(self.services.keys()))

    async def stop(self) -> None:
        """Stop supervisor and terminate all child processes."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        await self.stop_all(confirm="STOP_ALL")
        log.info("supervisor_stopped")

    async def start_service(self, name: str) -> bool:
        """Start a managed service process."""
        if name not in self.services:
            raise ValueError(f"Unknown service '{name}'; available: {list(self.services.keys())}")

        svc = self.services[name]
        if svc.status == "running" and svc.process and svc.process.returncode is None:
            return True

        svc.enabled = True
        try:
            log.info("starting_service", name=name, cmd=svc.command)
            proc = await asyncio.create_subprocess_exec(
                *svc.command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            svc.process = proc
            svc.pid = proc.pid
            svc.started_at = time.time()
            svc.status = "running"
            svc.backoff_delay = 1.0
            log.info("service_started", name=name, pid=svc.pid)
            return True
        except Exception as e:
            svc.status = "crashed"
            log.error("service_start_failed", name=name, error=str(e))
            return False

    async def stop_service(self, name: str) -> bool:
        """Stop a running managed service process."""
        if name not in self.services:
            raise ValueError(f"Unknown service '{name}'")

        svc = self.services[name]
        svc.enabled = False
        if not svc.process or svc.process.returncode is not None:
            svc.status = "stopped"
            svc.pid = None
            return True

        svc.status = "stopping"
        try:
            svc.process.terminate()
            try:
                await asyncio.wait_for(svc.process.wait(), timeout=5.0)
            except TimeoutError:
                svc.process.kill()
                await svc.process.wait()

            svc.status = "stopped"
            svc.pid = None
            svc.last_exit_code = svc.process.returncode
            log.info("service_stopped", name=name, exit_code=svc.last_exit_code)
            return True
        except Exception as e:
            log.error("service_stop_failed", name=name, error=str(e))
            svc.status = "crashed"
            return False

    async def restart_service(self, name: str) -> bool:
        """Restart a service process."""
        await self.stop_service(name)
        await asyncio.sleep(0.5)
        return await self.start_service(name)

    async def stop_all(self, confirm: str) -> dict[str, bool]:
        """Stop all running services. Requires typed confirmation 'STOP_ALL'."""
        if confirm != "STOP_ALL":
            raise ValueError("Typed confirmation 'STOP_ALL' is required to stop all services.")

        results = {}
        for name in list(self.services.keys()):
            results[name] = await self.stop_service(name)
        log.info("all_services_stopped", results=results)
        return results

    async def _monitor_loop(self) -> None:
        """Continuous watchdog checking child processes and restarting crashed enabled services."""
        while self._running:
            try:
                await asyncio.sleep(2.0)
                now = time.time()
                for name, svc in self.services.items():
                    if svc.status == "running" and svc.process:
                        if svc.started_at:
                            svc.uptime_s = round(now - svc.started_at, 1)

                        # Check if process died
                        ret = svc.process.returncode
                        if ret is not None:
                            svc.last_exit_code = ret
                            svc.pid = None
                            if svc.enabled:
                                svc.status = "crashed"
                                svc.restart_count += 1
                                log.warning(
                                    "service_crashed_scheduling_restart",
                                    name=name,
                                    exit_code=ret,
                                    restart_count=svc.restart_count,
                                    backoff_s=svc.backoff_delay,
                                )
                                # Restart with exponential backoff
                                task = asyncio.create_task(self._restart_with_backoff(svc))
                                self._background_tasks.add(task)
                                task.add_done_callback(self._background_tasks.discard)
                            else:
                                svc.status = "stopped"
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("supervisor_monitor_error", error=str(e))

    async def _restart_with_backoff(self, svc: ServiceState) -> None:
        delay = svc.backoff_delay
        svc.status = "restarting"
        await asyncio.sleep(delay)
        svc.backoff_delay = min(svc.backoff_delay * 2.0, 30.0)
        if svc.enabled and self._running:
            await self.start_service(svc.name)

    def get_status(self) -> dict[str, Any]:
        """Return structured status report for all managed services."""
        now = time.time()
        res = {}
        for name, svc in self.services.items():
            uptime = (
                round(now - svc.started_at, 1)
                if (svc.status == "running" and svc.started_at)
                else 0.0
            )
            res[name] = {
                "name": name,
                "status": svc.status,
                "pid": svc.pid,
                "uptime_s": uptime,
                "restart_count": svc.restart_count,
                "last_exit_code": svc.last_exit_code,
                "enabled": svc.enabled,
            }
        return res


_supervisor: SupervisorManager | None = None


def get_supervisor() -> SupervisorManager:
    global _supervisor
    if _supervisor is None:
        _supervisor = SupervisorManager()
    return _supervisor
