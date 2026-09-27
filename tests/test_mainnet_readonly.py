"""Unit tests for Binance Mainnet Read-Only Client.

All network calls are strictly mocked.
Verifies safety invariants:
1. Non-GET methods (POST, DELETE, PUT) blocked before network call.
2. Non-allowlisted endpoints blocked before network call.
3. API key with trading/margin/transfer/withdrawal permissions rejected at startup.
4. Graceful handling/skipping when futures permission is disabled.
5. Trading executors strictly refuse MainnetReadOnlyClient.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from deskmind.config import Settings
from deskmind.data.mainnet_readonly import (
    MainnetReadOnlyClient,
    MainnetReadOnlySecurityViolation,
    MainnetSecurityError,
)
from deskmind.execution.binance_testnet import BinanceTestnetExecutor


class MockTransport(httpx.AsyncBaseTransport):
    """Mock HTTP transport returning controlled responses."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests_log: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests_log.append(request)
        return self.handler(request)


def make_test_settings() -> Settings:
    return Settings(
        trading_mode="testnet",
        binance_mainnet_readonly_api_key=SecretStr("mock_readonly_key_12345"),
        binance_mainnet_readonly_api_secret=SecretStr("mock_readonly_secret_67890"),
        data_symbols=["BTCUSDT", "ETHUSDT"],
    )


# -----------------------------------------------------------------------------
# 1. Non-GET Methods Blocked Before Network Call
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_get_methods_blocked_before_network():
    transport = MockTransport(lambda r: httpx.Response(200, json={}))
    mock_http = httpx.AsyncClient(transport=transport)
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    for method in ("POST", "DELETE", "PUT", "PATCH"):
        with pytest.raises(MainnetReadOnlySecurityViolation) as exc_info:
            await client.request(method, "/sapi/v1/account/apiRestrictions")
        assert "strictly forbids non-GET method" in str(exc_info.value)

    # Verify zero network requests were dispatched
    assert len(transport.requests_log) == 0


# -----------------------------------------------------------------------------
# 2. Non-Allowlisted Endpoints Blocked Before Network Call
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_allowlisted_endpoints_blocked_before_network():
    transport = MockTransport(lambda r: httpx.Response(200, json={}))
    mock_http = httpx.AsyncClient(transport=transport)
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    forbidden_paths = [
        "/fapi/v1/order",
        "/fapi/v1/allOpenOrders",
        "/fapi/v1/algoOrder",
        "/api/v3/order",
        "/fapi/v1/positionSide/dual",
        "/sapi/v1/margin/order",
    ]

    for path in forbidden_paths:
        with pytest.raises(MainnetReadOnlySecurityViolation) as exc_info:
            await client.request("GET", path)
        assert "not in the mainnet read-only allowlist" in str(exc_info.value)

    # Verify zero network requests were dispatched
    assert len(transport.requests_log) == 0


# -----------------------------------------------------------------------------
# 3. Startup Permission Check Refuses Key With Trading / Transfer Permissions
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forbidden_flag",
    [
        "enableSpotAndMarginTrading",
        "enableWithdrawals",
        "enableInternalTransfer",
        "enableMargin",
        "permitsUniversalTransfer",
        "enableVanillaOptions",
    ],
)
async def test_startup_permission_check_refuses_dangerous_permissions(forbidden_flag: str):
    restrictions_payload = {
        "ipRestrict": True,
        "enableReading": True,
        "enableSpotAndMarginTrading": False,
        "enableWithdrawals": False,
        "enableInternalTransfer": False,
        "enableMargin": False,
        "permitsUniversalTransfer": False,
        "enableVanillaOptions": False,
        "enableFutures": True,
    }
    # Turn on the forbidden flag
    restrictions_payload[forbidden_flag] = True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sapi/v1/account/apiRestrictions"
        return httpx.Response(200, json=restrictions_payload)

    mock_http = httpx.AsyncClient(transport=MockTransport(handler))
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    with pytest.raises(MainnetSecurityError) as exc_info:
        await client.check_permissions()

    assert "forbidden" in str(exc_info.value).lower()
    assert forbidden_flag in str(exc_info.value)


# -----------------------------------------------------------------------------
# 4. Startup Permission Check Accepts Strictly Read-Only Key
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_permission_check_accepts_clean_readonly_key():
    clean_payload = {
        "ipRestrict": True,
        "enableReading": True,
        "enableSpotAndMarginTrading": False,
        "enableWithdrawals": False,
        "enableInternalTransfer": False,
        "enableMargin": False,
        "permitsUniversalTransfer": False,
        "enableVanillaOptions": False,
        "enableFutures": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sapi/v1/account/apiRestrictions"
        return httpx.Response(200, json=clean_payload)

    mock_http = httpx.AsyncClient(transport=MockTransport(handler))
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    perms = await client.check_permissions()
    assert perms["enableReading"] is True
    assert client.futures_enabled is True


# -----------------------------------------------------------------------------
# 5. Graceful Skip When Futures Permission Is Off
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_skip_when_futures_permission_disabled():
    no_futures_payload = {
        "ipRestrict": True,
        "enableReading": True,
        "enableSpotAndMarginTrading": False,
        "enableWithdrawals": False,
        "enableInternalTransfer": False,
        "enableMargin": False,
        "permitsUniversalTransfer": False,
        "enableVanillaOptions": False,
        "enableFutures": False,
    }

    transport = MockTransport(lambda r: httpx.Response(200, json=no_futures_payload))
    mock_http = httpx.AsyncClient(transport=transport)
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    await client.check_permissions()
    assert client.futures_enabled is False

    # All futures queries should gracefully skip and return empty/None without errors
    brackets = await client.get_leverage_brackets("BTCUSDT")
    assert brackets == []

    rate = await client.get_commission_rate("BTCUSDT")
    assert rate is None

    acct = await client.get_futures_account_info()
    assert acct is None

    # Only 1 request was sent (/sapi/v1/account/apiRestrictions); futures calls were skipped
    assert len(transport.requests_log) == 1


# -----------------------------------------------------------------------------
# 6. Graceful Handling When Futures API Call Returns Permission Error (-2015)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_skip_on_futures_api_permission_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(
                200,
                json={
                    "enableReading": True,
                    "enableSpotAndMarginTrading": False,
                    "enableWithdrawals": False,
                    "enableInternalTransfer": False,
                    "enableMargin": False,
                    "permitsUniversalTransfer": False,
                    "enableFutures": True,
                },
            )
        # Endpoint returns -2015 permission error
        return httpx.Response(
            401,
            json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."},
        )

    mock_http = httpx.AsyncClient(transport=MockTransport(handler))
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    await client.check_permissions()

    # Even though enableFutures was reported True, the specific endpoint rejects it
    # Client must handle it gracefully without crashing
    rate = await client.get_commission_rate("BTCUSDT")
    assert rate is None

    brackets = await client.get_leverage_brackets("BTCUSDT")
    assert brackets == []

    acct = await client.get_futures_account_info()
    assert acct is None


# -----------------------------------------------------------------------------
# 7. Trading Executors Cannot Use MainnetReadOnlyClient
# -----------------------------------------------------------------------------


def test_executors_cannot_use_mainnet_readonly_client():
    client = MainnetReadOnlyClient(settings=make_test_settings())

    with pytest.raises(TypeError) as exc_info:
        BinanceTestnetExecutor(client=client)  # type: ignore[arg-type]

    assert "cannot use MainnetReadOnlyClient" in str(exc_info.value)


# -----------------------------------------------------------------------------
# 8. Full Account Sync Mocked Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_account_mocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(
                200,
                json={
                    "enableReading": True,
                    "enableSpotAndMarginTrading": False,
                    "enableWithdrawals": False,
                    "enableInternalTransfer": False,
                    "enableMargin": False,
                    "permitsUniversalTransfer": False,
                    "enableFutures": True,
                },
            )
        if path == "/fapi/v1/leverageBracket":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "brackets": [
                            {
                                "bracket": 1,
                                "initialLeverage": 125,
                                "notionalCap": 50000.0,
                                "notionalFloor": 0.0,
                                "maintMarginRatio": 0.005,
                                "cum": 0.0,
                            }
                        ],
                    }
                ],
            )
        if path == "/fapi/v1/commissionRate":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0005",
                },
            )
        if path == "/fapi/v2/account":
            return httpx.Response(
                200,
                json={"totalWalletBalance": "15000.0", "totalMarginBalance": "15000.0"},
            )
        return httpx.Response(404, json={"code": -1000, "msg": "Not found"})

    mock_http = httpx.AsyncClient(transport=MockTransport(handler))
    client = MainnetReadOnlyClient(settings=make_test_settings(), http_client=mock_http)

    report = await client.sync_account()

    assert report["source"] == "binance_mainnet_readonly"
    assert "BTCUSDT" in report["brackets"]
    assert "BTCUSDT" in report["commission_rates"]
    assert report["account_info"]["totalWalletBalance"] == "15000.0"

    # Verify comparison calculations
    fee_diffs = report["comparison"]["fee_differences"]
    assert len(fee_diffs) >= 1
    assert fee_diffs[0]["symbol"] == "BTCUSDT"
    assert fee_diffs[0]["maker_diff"] == "0.0000"
    assert fee_diffs[0]["taker_diff"] == "0.0000"

    mmr_diffs = report["comparison"]["mmr_differences"]
    assert len(mmr_diffs) >= 1
    assert mmr_diffs[0]["symbol"] == "BTCUSDT"
    assert mmr_diffs[0]["diff"] == "0.000"

    # Verify snapshot file was created
    meta_files = list((tmp_path / "data" / "meta").glob("mainnet_sync_*.json"))
    assert len(meta_files) == 1
