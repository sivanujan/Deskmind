from datetime import date

import httpx
import pytest

from deskmind.data import bulk_download as bulk
from deskmind.data.datasets import FUNDING, HOUR_MS, Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.storage import time_values_ms
from tests.data_fixtures import FakeClock, checksum_file, kline_row, make_zip

H1 = Dataset("klines", "1h")
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC


def vision(files: dict[str, bytes]) -> tuple[RateLimitedClient, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = files.get(request.url.path)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    clock = FakeClock()
    return (
        RateLimitedClient(
            "https://vision.test",
            weight_limit=None,
            transport=httpx.MockTransport(handler),
            sleep=clock.sleep,
            clock=clock,
        ),
        seen,
    )


def test_urls() -> None:
    assert bulk.monthly_url("BTCUSDT", H1, 2024, 1) == (
        "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    )
    assert bulk.monthly_url("BTCUSDT", Dataset("mark_klines", "5m"), 2023, 12) == (
        "/data/futures/um/monthly/markPriceKlines/BTCUSDT/5m/BTCUSDT-5m-2023-12.zip"
    )
    assert bulk.monthly_url("BTCUSDT", FUNDING, 2023, 1) == (
        "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2023-01.zip"
    )
    assert bulk.daily_url("BTCUSDT", H1, date(2024, 1, 5)) == (
        "/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01-05.zip"
    )
    assert bulk.daily_url("BTCUSDT", FUNDING, date(2024, 1, 5)) is None


async def test_verified_zip_ok() -> None:
    rows = [kline_row(T0 + i * HOUR_MS, HOUR_MS) for i in range(3)]
    z = make_zip("a.csv", rows)
    client, _ = vision({"/f.zip": z, "/f.zip.CHECKSUM": checksum_file(z, "f.zip")})
    assert await bulk.fetch_verified_zip(client, "/f.zip") == z


async def test_checksum_mismatch_raises_after_retry() -> None:
    z = make_zip("a.csv", [kline_row(T0, HOUR_MS)])
    client, seen = vision({"/f.zip": z, "/f.zip.CHECKSUM": b"0" * 64 + b"  f.zip"})
    with pytest.raises(bulk.ChecksumError, match="mismatch"):
        await bulk.fetch_verified_zip(client, "/f.zip")
    assert seen.count("/f.zip") == 2  # re-downloaded once before giving up


async def test_missing_checksum_file_is_an_error() -> None:
    z = make_zip("a.csv", [kline_row(T0, HOUR_MS)])
    client, _ = vision({"/f.zip": z})
    with pytest.raises(bulk.ChecksumError, match="no CHECKSUM"):
        await bulk.fetch_verified_zip(client, "/f.zip")


async def test_missing_zip_returns_none() -> None:
    client, _ = vision({})
    assert await bulk.fetch_verified_zip(client, "/nope.zip") is None


def test_csv_with_and_without_header_parse_identically() -> None:
    rows = [kline_row(T0 + i * HOUR_MS, HOUR_MS) for i in range(5)]
    with_header = bulk.read_zip_rows(make_zip("a.csv", rows))
    without = bulk.read_zip_rows(make_zip("a.csv", rows, header=None))
    assert with_header == without == rows


def test_microsecond_timestamps_are_normalised() -> None:
    row = kline_row(T0, HOUR_MS)
    row[0] = str(int(row[0]) * 1000)
    row[6] = str(int(row[6]) * 1000)
    tbl = bulk.rows_to_table(H1, [row])
    assert time_values_ms(tbl, "open_time") == [T0]


def test_funding_times_rounded_to_second() -> None:
    rows = [["1672531200000", "8", "0.0001"], ["1672560000008", "8", "0.0002"]]
    tbl = bulk.rows_to_table(FUNDING, rows)
    assert time_values_ms(tbl, "funding_time") == [1672531200000, 1672560000000]
    assert tbl.column("funding_interval_hours").to_pylist() == [8, 8]
