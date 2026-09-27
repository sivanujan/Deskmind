"""User Data Stream WebSocket manager for Binance USDT-M Futures."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import websockets

from deskmind.config import ALLOWED_BINANCE_FUTURES_WS_HOSTS
from deskmind.execution.binance_client import BinanceFuturesClient
from deskmind.logging import get_logger

log = get_logger(__name__)


class UserStreamManager:
    """Manages the Binance Futures user data stream WebSocket and keepalive lifecycle."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        ws_base_url: str = "wss://demo-fstream.binance.com",
        keepalive_interval_s: float = 1800.0,  # 30 minutes
    ) -> None:
        self.client = client
        self.ws_base_url = self._validate_ws_host(ws_base_url)
        self.keepalive_interval_s = keepalive_interval_s

        self.listen_key: str = ""
        self._running = False
        self._ws: Any = None
        self._ws_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

        self._order_callbacks: list[Callable[[dict[str, Any]], Any]] = []
        self._account_callbacks: list[Callable[[dict[str, Any]], Any]] = []

        # Fault injection hooks
        self._suppress_keepalive = False

    @staticmethod
    def _validate_ws_host(url: str) -> str:
        parsed = urlparse(url.strip())
        host = parsed.hostname or ""
        if parsed.scheme != "wss" or host not in ALLOWED_BINANCE_FUTURES_WS_HOSTS:
            raise ValueError(
                f"SECURITY VIOLATION: Refusing WS connection to host {host!r}. "
                f"Allowed testnet hosts: {ALLOWED_BINANCE_FUTURES_WS_HOSTS}"
            )
        return url.strip().rstrip("/")

    def on_order_update(self, cb: Callable[[dict[str, Any]], Any]) -> None:
        self._order_callbacks.append(cb)

    def on_account_update(self, cb: Callable[[dict[str, Any]], Any]) -> None:
        self._account_callbacks.append(cb)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not getattr(self._ws, "closed", True)

    async def get_listen_key(self) -> str:
        if not self.listen_key:
            await self._acquire_listen_key()
        return self.listen_key

    async def keepalive(self) -> bool:
        if not self.listen_key:
            return False
        await self.client.request(
            "PUT",
            "/fapi/v1/listenKey",
            params={"listenKey": self.listen_key},
            signed=False,
            weight=1,
        )
        return True

    async def start(self) -> None:
        """Obtain listenKey, start keepalive, and launch WebSocket listener."""
        if self._running:
            return
        self._running = True
        await self._acquire_listen_key()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._ws_task = asyncio.create_task(self._stream_loop())
        log.info("user_stream_started", ws_url=self.ws_base_url)

    async def _acquire_listen_key(self) -> None:
        """Create a new listenKey."""
        data = await self.client.request("POST", "/fapi/v1/listenKey", signed=False, weight=1)
        self.listen_key = data.get("listenKey", "")
        if not self.listen_key:
            raise RuntimeError("Failed to obtain listenKey from Binance Futures")
        log.info("listen_key_acquired")

    async def _keepalive_loop(self) -> None:
        """Ping listenKey every 30 minutes to keep stream alive."""
        while self._running:
            try:
                await asyncio.sleep(self.keepalive_interval_s)
                if not self._running:
                    break
                if self._suppress_keepalive:
                    log.warning("fault_injection: user_stream keepalive suppressed")
                    continue
                if self.listen_key:
                    await self.client.request(
                        "PUT",
                        "/fapi/v1/listenKey",
                        params={"listenKey": self.listen_key},
                        signed=False,
                        weight=1,
                    )
                    log.debug("listen_key_pinged")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("listen_key_keepalive_error", error=str(e))

    async def _stream_loop(self) -> None:
        """Connect to WebSocket and listen for events with auto-reconnect."""
        backoff = 1.0
        while self._running:
            ws_url = f"{self.ws_base_url}/ws/{self.listen_key}"
            try:
                log.info("connecting_user_stream_ws")
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("user_stream_ws_connected")
                    async for msg_str in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(msg_str)
                            await self._dispatch(msg)
                        except Exception as e:
                            log.error("user_stream_dispatch_error", error=str(e))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("user_stream_ws_disconnected", error=str(e), reconnect_in=backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)
                # Try refreshing listenKey on reconnect
                try:
                    await self._acquire_listen_key()
                except Exception as lk_err:
                    log.warning("reacquire_listen_key_failed", error=str(lk_err))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("e")
        if event_type == "ORDER_TRADE_UPDATE":
            for cb in self._order_callbacks:
                try:
                    res = cb(msg.get("o", {}))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    log.error("order_callback_error", error=str(e))
        elif event_type == "ACCOUNT_UPDATE":
            for cb in self._account_callbacks:
                try:
                    res = cb(msg.get("a", {}))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    log.error("account_callback_error", error=str(e))

    # --------------------------------------------------------------------------
    # Fault Injection Hooks
    # --------------------------------------------------------------------------

    def stop_keepalive_fault(self) -> None:
        """Inject fault: halt listenKey keepalive."""
        self._suppress_keepalive = True
        log.warning("fault_injected: user_stream_keepalive_stopped")

    async def force_disconnect_fault(self) -> None:
        """Inject fault: forcefully close active WebSocket."""
        log.warning("fault_injected: user_stream_force_disconnect")
        if self._ws is not None:
            await self._ws.close()

    async def stop(self) -> None:
        """Stop keepalive, close WS, and delete listenKey."""
        self._running = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        if self.listen_key:
            try:
                await self.client.request(
                    "DELETE",
                    "/fapi/v1/listenKey",
                    params={"listenKey": self.listen_key},
                    signed=False,
                    weight=1,
                )
            except Exception as e:
                log.debug("delete_listen_key_info", error=str(e))
        log.info("user_stream_stopped")
