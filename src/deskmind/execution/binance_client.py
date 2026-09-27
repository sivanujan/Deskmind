"""Authenticated, rate-limited async client for Binance USDT-M Futures Testnet."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from deskmind.config import ALLOWED_BINANCE_FUTURES_REST_HOSTS
from deskmind.data.http_client import WeightTracker
from deskmind.logging import get_logger

log = get_logger(__name__)


class BinanceAPIException(Exception):
    """Exception raised for Binance Futures API errors."""

    def __init__(self, code: int, message: str, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"Binance API Error {code}: {message} (HTTP {status_code})")


class BinanceFuturesClient:
    """Authenticated client for Binance Futures REST endpoints.

    Strictly refuses any non-testnet hosts on initialization and on every request.
    API keys and secrets are never logged or exposed.
    """

    def __init__(
        self,
        base_url: str = "https://demo-fapi.binance.com",
        api_key: str = "",
        secret_key: str = "",
        weight_tracker: WeightTracker | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = self._validate_testnet_host(base_url)
        self._api_key = api_key.strip()
        self._secret_key = secret_key.strip()
        self.weight_tracker = weight_tracker or WeightTracker()
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    @staticmethod
    def _validate_testnet_host(url: str) -> str:
        """Strictly enforce testnet host allowlist."""
        parsed = urlparse(url.strip())
        host = parsed.hostname or ""
        if parsed.scheme != "https" or host not in ALLOWED_BINANCE_FUTURES_REST_HOSTS:
            raise ValueError(
                f"SECURITY VIOLATION: Refusing execution to host {host!r}. "
                f"Allowed testnet hosts: {ALLOWED_BINANCE_FUTURES_REST_HOSTS}"
            )
        return url.strip().rstrip("/")

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """Sign request parameters with HMAC-SHA256."""
        now_ms = int(time.time() * 1000)
        params["timestamp"] = now_ms
        if "recvWindow" not in params:
            params["recvWindow"] = 5000

        query_str = urlencode([(k, v) for k, v in params.items() if v is not None])
        sig = hmac.new(
            self._secret_key.encode("utf-8"),
            query_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self._api_key:
            headers["X-MBX-APIKEY"] = self._api_key
        return headers

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        weight: int = 1,
    ) -> Any:
        """Send authenticated or public request to Binance Futures testnet."""
        # Security invariant: validate host on every single request
        self._validate_testnet_host(self.base_url)

        req_params = dict(params or {})
        if signed:
            if not self._api_key or not self._secret_key:
                raise ValueError("API key and secret required for signed Binance requests")
            req_params = self._sign(req_params)

        await self.weight_tracker.acquire(weight)

        url = f"{self.base_url}{path}"
        headers = self._headers()

        try:
            if method.upper() == "GET":
                resp = await self._client.get(url, params=req_params, headers=headers)
            elif method.upper() == "POST":
                resp = await self._client.post(url, data=req_params, headers=headers)
            elif method.upper() == "PUT":
                resp = await self._client.put(url, data=req_params, headers=headers)
            elif method.upper() == "DELETE":
                resp = await self._client.delete(url, params=req_params, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method {method}")
        except httpx.HTTPError as e:
            log.error("binance_network_error", path=path, error=type(e).__name__)
            raise

        self.weight_tracker.observe(resp.headers.get("X-MBX-USED-WEIGHT-1M"))

        if resp.status_code >= 400:
            try:
                err_data = resp.json()
                code = err_data.get("code", resp.status_code)
                msg = err_data.get("msg", resp.text)
            except Exception:
                code = resp.status_code
                msg = resp.text

            log.warning(
                "binance_api_error",
                path=path,
                status_code=resp.status_code,
                error_code=code,
                error_msg=msg,
            )
            raise BinanceAPIException(code=code, message=msg, status_code=resp.status_code)

        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
