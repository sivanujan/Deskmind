import httpx
import pytest

from deskmind.data.http_client import (
    BinanceHTTPError,
    BinanceIPBanned,
    RateLimitedClient,
    WeightTracker,
)
from tests.data_fixtures import FakeClock


def client_for(
    responses: list[httpx.Response], clock: FakeClock, **kw
) -> tuple[RateLimitedClient, list]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]

    client = RateLimitedClient(
        "https://fapi.test",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
        **kw,
    )
    return client, calls


async def test_429_honours_retry_after_then_succeeds() -> None:
    clock = FakeClock()
    client, calls = client_for(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": 1})],
        clock,
    )
    assert await client.get_json("/x") == {"ok": 1}
    assert len(calls) == 2
    assert 7.0 in clock.sleeps


async def test_418_stops_immediately() -> None:
    clock = FakeClock()
    client, calls = client_for([httpx.Response(418, text="banned")], clock)
    with pytest.raises(BinanceIPBanned):
        await client.get_json("/x")
    assert len(calls) == 1
    assert clock.sleeps == []


async def test_5xx_retries_with_backoff() -> None:
    clock = FakeClock()
    client, calls = client_for(
        [httpx.Response(502), httpx.Response(503), httpx.Response(200, json=[1])], clock
    )
    assert await client.get_json("/x") == [1]
    assert len(calls) == 3
    assert len(clock.sleeps) == 2 and clock.sleeps[1] > clock.sleeps[0] * 1.2


async def test_5xx_gives_up_after_max_retries() -> None:
    clock = FakeClock()
    client, calls = client_for([httpx.Response(500)], clock, max_retries=2)
    with pytest.raises(BinanceHTTPError):
        await client.get_json("/x")
    assert len(calls) == 3


async def test_4xx_is_not_retried() -> None:
    clock = FakeClock()
    client, calls = client_for([httpx.Response(400, json={"code": -1121})], clock)
    with pytest.raises(BinanceHTTPError) as exc:
        await client.get_json("/x")
    assert exc.value.status == 400
    assert len(calls) == 1


async def test_get_bytes_404_returns_none() -> None:
    clock = FakeClock()
    client, _ = client_for([httpx.Response(404)], clock)
    assert await client.get_bytes("/missing.zip") is None


async def test_transport_errors_retry() -> None:
    clock = FakeClock()
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    client = RateLimitedClient(
        "https://fapi.test", transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock
    )
    assert await client.get_json("/x") == {"ok": True}
    assert n["calls"] == 2


async def test_weight_tracker_waits_for_next_minute() -> None:
    clock = FakeClock(t=60 * 1000 + 10)  # 10s into a minute
    tracker = WeightTracker(limit=100, safety=0.5, clock=clock, sleep=clock.sleep)  # budget 50
    for _ in range(5):
        await tracker.acquire(10)
    assert clock.sleeps == []
    await tracker.acquire(10)  # would exceed 50 -> wait ~50.5s for the next minute
    assert len(clock.sleeps) == 1 and 50 <= clock.sleeps[0] <= 51
    assert tracker.used == 10


async def test_weight_header_raises_local_count() -> None:
    clock = FakeClock(t=60 * 1000)
    tracker = WeightTracker(limit=100, safety=0.5, clock=clock, sleep=clock.sleep)
    tracker.observe("48")
    await tracker.acquire(5)  # 48 + 5 > 50 -> must wait
    assert len(clock.sleeps) == 1


async def test_client_tracks_server_reported_weight() -> None:
    clock = FakeClock(t=60 * 1000)
    client, _ = client_for(
        [httpx.Response(200, json=[], headers={"X-MBX-USED-WEIGHT-1M": "1500"})], clock
    )
    await client.get_json("/x", weight=5)
    assert client.tracker is not None and client.tracker.used == 1500
