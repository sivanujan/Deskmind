"""Binance Mainnet Read-Only Client.

Strictly read-only access to Binance mainnet for metadata sync and verification:
- API key restrictions check (/sapi/v1/account/apiRestrictions)
- Leverage brackets (/fapi/v1/leverageBracket)
- Commission rates (/fapi/v1/commissionRate)
- Futures account info (/fapi/v1/account or /fapi/v2/account)

Safety Invariants:
1. Only signed GET requests on explicitly allowlisted paths are permitted.
2. Any non-GET method or non-allowlisted endpoint raises MainnetReadOnlySecurityViolation
   BEFORE any network call is dispatched.
3. Startup permission check: if the key has ANY trading, futures-trading, margin,
   transfer, or withdrawal permissions enabled, execution is immediately refused with CRITICAL log.
4. If futures permission is disabled, futures read endpoints are skipped gracefully.
5. This client is NEVER usable by any trading executor (enforced by type and attribute assertions).
6. Secrets are never printed or logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from deskmind.config import (
    DEFAULT_MAINTENANCE_BRACKETS,
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    Settings,
    get_settings,
)
from deskmind.logging import get_logger

log = get_logger(__name__)

ALLOWLISTED_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/sapi/v1/account/apiRestrictions",
        "/fapi/v1/leverageBracket",
        "/fapi/v1/commissionRate",
        "/fapi/v1/account",
        "/fapi/v2/account",
        # Trade-history import (read-only, USER_DATA GETs; checked against current Binance docs):
        "/fapi/v1/userTrades",  # futures fills per symbol, <= 7-day windows, last 3 months only
        "/fapi/v1/income",  # futures income (REALIZED_PNL, COMMISSION, ...), last 3 months only
        "/api/v3/myTrades",  # spot fills per symbol, paged by fromId (full history)
        "/api/v3/account",  # spot balances: used only to discover which spot symbols to query
        "/api/v3/time",  # public server time, unsigned: aligns request timestamps (-1021)
    }
)

FUTURES_NOT_ENABLED = "Needs futures read access — not enabled (by design)"


class FuturesReadNotEnabled(Exception):
    """Futures read endpoints refused or skipped because the key has no futures permission."""


FORBIDDEN_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        "enablespotandmargintrading",
        "enablewithdrawals",
        "enableinternaltransfer",
        "enablemargin",
        "permitsuniversaltransfer",
        "enablevanillaoptions",
    }
)


class MainnetReadOnlySecurityViolation(Exception):
    """Raised when an illegal HTTP method or non-allowlisted endpoint is requested."""


class MainnetSecurityError(Exception):
    """Raised when key fails startup permission checks or possesses dangerous permissions."""


class MainnetReadOnlyClient:
    """Binance Mainnet Read-Only HTTP client.

    Designed exclusively for syncing market metadata (brackets, fee tiers) and read-only
    account info. Strictly prevents any mutation or trading calls.
    """

    IS_MAINNET_READONLY: bool = True
    is_mainnet_readonly: bool = True

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._http_client = http_client
        self._owns_client = http_client is None
        self.futures_enabled: bool = True
        self.permissions: dict[str, Any] | None = None
        # Binance server time minus local time; a fast local clock otherwise gets -1021.
        self.time_offset_ms: int | None = None

    @property
    def _api_key(self) -> str:
        return self.settings.binance_mainnet_readonly_api_key.get_secret_value()

    @property
    def _api_secret(self) -> str:
        return self.settings.binance_mainnet_readonly_api_secret.get_secret_value()

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add timestamp and compute HMAC-SHA256 signature."""
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000) + (self.time_offset_ms or 0)
        p.setdefault("recvWindow", 10_000)
        query_string = urlencode(p)
        secret_bytes = self._api_secret.encode("utf-8")
        signature = hmac.new(secret_bytes, query_string.encode("utf-8"), hashlib.sha256).hexdigest()
        p["signature"] = signature
        return p

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> Any:
        """Execute validated and signed GET request to Binance mainnet.

        Raises MainnetReadOnlySecurityViolation immediately if method != 'GET'
        or path is not in ALLOWLISTED_ENDPOINTS.
        """
        # 1. Method check: strictly GET only
        norm_method = method.strip().upper()
        if norm_method != "GET":
            msg = (
                f"SECURITY VIOLATION: Mainnet read-only client strictly forbids "
                f"non-GET method: {norm_method}"
            )
            log.critical("mainnet_readonly_method_violation", method=norm_method, path=path)
            raise MainnetReadOnlySecurityViolation(msg)

        # 2. Path check: strictly allowlisted paths only
        if path not in ALLOWLISTED_ENDPOINTS:
            msg = (
                f"SECURITY VIOLATION: Endpoint '{path}' is not in the mainnet "
                f"read-only allowlist: {sorted(ALLOWLISTED_ENDPOINTS)}"
            )
            log.critical("mainnet_readonly_path_violation", path=path)
            raise MainnetReadOnlySecurityViolation(msg)

        # 3. Determine base URL (spot /api and /sapi on api.binance.com, futures on fapi)
        if path.startswith(("/sapi/", "/api/")):
            base_url = self.settings.binance_mainnet_api_url.rstrip("/")
        else:
            base_url = self.settings.binance_mainnet_futures_url.rstrip("/")

        # 4. Prepare parameters and signature
        req_params = dict(params or {})
        if signed:
            req_params = self._sign_params(req_params)

        headers = {
            "X-MBX-APIKEY": self._api_key,
            "User-Agent": "Deskmind-Mainnet-ReadOnly/1.0",
        }

        url = f"{base_url}{path}"
        client = await self._get_client()

        resp = await client.request(
            method="GET",
            url=url,
            params=req_params,
            headers=headers,
        )

        # Handle HTTP error responses
        if resp.status_code >= 400:
            error_data = {}
            with contextlib.suppress(Exception):
                error_data = resp.json()
            code = error_data.get("code")
            msg = error_data.get("msg", resp.text)

            # Check for futures permission rejection (-2015, -1021, etc.)
            is_perm_err = (
                code in (-2015, -1021)
                or "permission" in msg.lower()
                or "not enabled" in msg.lower()
            )
            if is_perm_err:
                log.warning(
                    "mainnet_futures_permission_rejected",
                    path=path,
                    code=code,
                    msg=msg,
                )
            resp.raise_for_status()

        return resp.json()

    async def sync_time(self) -> int:
        """Measure Binance server time vs the local clock (public, unsigned GET)."""
        before = int(time.time() * 1000)
        data = await self.request("GET", "/api/v3/time", signed=False)
        after = int(time.time() * 1000)
        self.time_offset_ms = int(data["serverTime"]) - (before + after) // 2
        if abs(self.time_offset_ms) > 1000:
            log.warning("local_clock_drift_vs_binance", offset_ms=self.time_offset_ms)
        return self.time_offset_ms

    async def check_permissions(self) -> dict[str, Any]:
        """Verify API key restrictions on Binance mainnet.

        Refuses execution and raises MainnetSecurityError if ANY trading,
        margin, transfer, or withdrawal permissions are active.
        """
        log.info("checking_mainnet_api_restrictions")
        await self.sync_time()
        data = await self.request("GET", "/sapi/v1/account/apiRestrictions", signed=True)
        self.permissions = data

        # Check for forbidden permissions
        violations = []
        for k, v in data.items():
            if v is True:
                k_norm = k.lower().replace("_", "")
                if k_norm in FORBIDDEN_PERMISSION_KEYS or any(
                    bad in k_norm for bad in ("trading", "withdraw", "transfer", "margin")
                ):
                    violations.append(k)

        if violations:
            msg = (
                f"CRITICAL SECURITY REJECTION: Mainnet read-only API key has forbidden "
                f"write/trading/transfer permissions enabled: {violations}. "
                f"Key must be strictly read-only!"
            )
            log.critical(
                "mainnet_key_forbidden_permissions_detected",
                violations=violations,
            )
            raise MainnetSecurityError(msg)

        # Track futures read capability
        self.futures_enabled = bool(data.get("enableFutures", True))
        if not self.futures_enabled:
            log.warning(
                "mainnet_key_futures_disabled",
                note="enableFutures is False; futures read endpoints will be skipped gracefully",
            )

        log.info(
            "mainnet_api_restrictions_verified",
            enable_reading=data.get("enableReading", True),
            enable_futures=self.futures_enabled,
        )
        return data

    async def get_leverage_brackets(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch leverage bracket information for a symbol or all symbols."""
        if not self.futures_enabled:
            log.info("skip_leverage_brackets_futures_disabled")
            return []

        params = {"symbol": symbol} if symbol else {}
        try:
            res = await self.request("GET", "/fapi/v1/leverageBracket", params=params, signed=True)
            return res if isinstance(res, list) else [res]
        except Exception as e:
            log.warning("get_leverage_brackets_error", error=str(e), symbol=symbol)
            return []

    async def get_commission_rate(self, symbol: str) -> dict[str, Any] | None:
        """Fetch user commission rate for a futures symbol."""
        if not self.futures_enabled:
            log.info("skip_commission_rate_futures_disabled", symbol=symbol)
            return None

        try:
            return await self.request(
                "GET", "/fapi/v1/commissionRate", params={"symbol": symbol}, signed=True
            )
        except Exception as e:
            log.warning("get_commission_rate_error", error=str(e), symbol=symbol)
            return None

    async def get_futures_account_info(self) -> dict[str, Any] | None:
        """Fetch futures account information (read-only balance/margin overview)."""
        if not self.futures_enabled:
            log.info("skip_account_info_futures_disabled")
            return None

        try:
            return await self.request("GET", "/fapi/v2/account", signed=True)
        except Exception:
            try:
                return await self.request("GET", "/fapi/v1/account", signed=True)
            except Exception as e:
                log.warning("get_futures_account_info_error", error=str(e))
                return None

    # ------------------------------------------------------------ trade history (read-only)

    async def _futures_get(self, path: str, params: dict[str, Any]) -> Any:
        """Futures GET that turns a permission refusal into FuturesReadNotEnabled."""
        if not self.futures_enabled:
            raise FuturesReadNotEnabled(FUTURES_NOT_ENABLED)
        try:
            return await self.request("GET", path, params=params, signed=True)
        except httpx.HTTPStatusError as exc:
            body: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                body = exc.response.json()
            msg = str(body.get("msg", "")).lower()
            if (
                exc.response.status_code in (401, 403)
                or body.get("code") == -2015
                or ("permission" in msg or "not enabled" in msg)
            ):
                self.futures_enabled = False
                raise FuturesReadNotEnabled(FUTURES_NOT_ENABLED) from exc
            raise

    async def get_futures_income(
        self, start_ms: int, end_ms: int, income_type: str | None = None
    ) -> list[dict[str, Any]]:
        """All futures income rows in [start, end] (paged; Binance keeps ~3 months)."""
        out: list[dict[str, Any]] = []
        cursor = start_ms
        while cursor <= end_ms:
            params: dict[str, Any] = {"startTime": cursor, "endTime": end_ms, "limit": 1000}
            if income_type:
                params["incomeType"] = income_type
            rows = await self._futures_get("/fapi/v1/income", params)
            if not rows:
                break
            out.extend(rows)
            if len(rows) < 1000:
                break
            cursor = int(rows[-1]["time"]) + 1
        return out

    async def get_futures_user_trades(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        """Futures fills for one symbol, walking 7-day windows (Binance's maximum)."""
        week = 7 * 86_400_000
        out: list[dict[str, Any]] = []
        w_start = start_ms
        while w_start <= end_ms:
            w_end = min(w_start + week - 1, end_ms)
            cursor = w_start
            while True:
                rows = await self._futures_get(
                    "/fapi/v1/userTrades",
                    {"symbol": symbol, "startTime": cursor, "endTime": w_end, "limit": 1000},
                )
                out.extend(rows)
                if len(rows) < 1000:
                    break
                cursor = int(rows[-1]["time"]) + 1
            w_start = w_end + 1
        return out

    async def get_spot_balances(self) -> list[dict[str, Any]]:
        data = await self.request("GET", "/api/v3/account", params={"omitZeroBalances": "true"})
        return list(data.get("balances", []))

    async def get_spot_my_trades(self, symbol: str) -> list[dict[str, Any]]:
        """Full spot fill history for one symbol, paged by trade id (fromId)."""
        out: list[dict[str, Any]] = []
        from_id = 0
        while True:
            rows = await self.request(
                "GET",
                "/api/v3/myTrades",
                params={"symbol": symbol, "fromId": from_id, "limit": 1000},
            )
            out.extend(rows)
            if len(rows) < 1000:
                break
            from_id = int(rows[-1]["id"]) + 1
        return out

    async def sync_account(self) -> dict[str, Any]:
        """Perform full read-only metadata synchronization and configuration comparison."""
        now_dt = datetime.now(UTC)
        date_str = now_dt.strftime("%Y%m%d_%H%M%S")

        # 1. Startup permission check
        permissions = await self.check_permissions()

        # 2. Fetch leverage brackets and commission rates for configured symbols
        symbols = self.settings.data_symbols
        brackets_by_sym: dict[str, list[dict[str, Any]]] = {}
        commission_by_sym: dict[str, dict[str, Any]] = {}

        raw_brackets = await self.get_leverage_brackets()
        for b in raw_brackets:
            sym = b.get("symbol")
            if sym in symbols:
                brackets_by_sym[sym] = b.get("brackets", [])

        # Fetch commission rates per symbol
        for sym in symbols:
            rate = await self.get_commission_rate(sym)
            if rate:
                commission_by_sym[sym] = rate

        # Fetch account info
        account_info = await self.get_futures_account_info()

        # 3. Analyze differences against Deskmind configuration
        fee_diffs = []
        for sym in symbols:
            rate_data = commission_by_sym.get(sym)
            if rate_data:
                live_maker = Decimal(str(rate_data.get("makerCommissionRate", "0")))
                live_taker = Decimal(str(rate_data.get("takerCommissionRate", "0")))
                diff = {
                    "symbol": sym,
                    "config_maker": str(DEFAULT_MAKER_FEE_RATE),
                    "live_maker": str(live_maker),
                    "maker_diff": str(live_maker - DEFAULT_MAKER_FEE_RATE),
                    "config_taker": str(DEFAULT_TAKER_FEE_RATE),
                    "live_taker": str(live_taker),
                    "taker_diff": str(live_taker - DEFAULT_TAKER_FEE_RATE),
                }
                fee_diffs.append(diff)

        mmr_diffs = []
        for sym, b_list in brackets_by_sym.items():
            if b_list:
                tier1 = b_list[0]
                live_mmr = Decimal(str(tier1.get("maintMarginRatio", "0")))
                config_tier1_mmr = DEFAULT_MAINTENANCE_BRACKETS[0][1]
                mmr_diffs.append(
                    {
                        "symbol": sym,
                        "tier": 1,
                        "config_mmr": str(config_tier1_mmr),
                        "live_mmr": str(live_mmr),
                        "diff": str(live_mmr - config_tier1_mmr),
                        "max_leverage": tier1.get("initialLeverage"),
                    }
                )

        report = {
            "synced_at": now_dt.isoformat(),
            "source": "binance_mainnet_readonly",
            "permissions": permissions,
            "symbols": symbols,
            "commission_rates": commission_by_sym,
            "brackets": brackets_by_sym,
            "account_info": account_info,
            "comparison": {
                "fee_differences": fee_diffs,
                "mmr_differences": mmr_diffs,
            },
        }

        # 4. Save metadata snapshot
        def _save_snapshot() -> Path:
            out_dir = Path("data/meta")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"mainnet_sync_{date_str}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            return out_path

        saved_path = await asyncio.to_thread(_save_snapshot)
        log.info("mainnet_sync_snapshot_saved", path=str(saved_path))

        return report

    async def close(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
