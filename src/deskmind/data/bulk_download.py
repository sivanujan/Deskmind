"""Bulk history from data.binance.vision (USDT-M futures), verified against .CHECKSUM files."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import date

import pyarrow as pa

from deskmind.data.datasets import Dataset
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.storage import candles_table, funding_table
from deskmind.logging import get_logger

log = get_logger(__name__)

VISION_BASE_URL = "https://data.binance.vision"
_PREFIX = "/data/futures/um"


class ChecksumError(Exception):
    """Zip is missing its CHECKSUM file or does not match it."""


def monthly_url(symbol: str, ds: Dataset, year: int, month: int) -> str:
    period = f"{year:04d}-{month:02d}"
    if ds.kind == "funding":
        return f"{_PREFIX}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{period}.zip"
    return (
        f"{_PREFIX}/monthly/{ds.bulk_folder}/{symbol}/{ds.timeframe}/"
        f"{symbol}-{ds.timeframe}-{period}.zip"
    )


def daily_url(symbol: str, ds: Dataset, day: date) -> str | None:
    """Funding has no daily files on data.binance.vision."""
    if ds.kind == "funding":
        return None
    return (
        f"{_PREFIX}/daily/{ds.bulk_folder}/{symbol}/{ds.timeframe}/"
        f"{symbol}-{ds.timeframe}-{day.isoformat()}.zip"
    )


def parse_checksum(text: bytes) -> str:
    parts = text.decode("utf-8", errors="replace").split()
    if not parts or len(parts[0]) != 64:
        raise ChecksumError(f"unparseable CHECKSUM content: {text[:100]!r}")
    return parts[0].lower()


async def fetch_verified_zip(client: RateLimitedClient, url: str) -> bytes | None:
    """Download a zip and its CHECKSUM; None if the zip does not exist (404).

    A missing CHECKSUM or a mismatch (after one re-download) raises ChecksumError: unverified
    data is never used.
    """
    for attempt in range(2):
        data = await client.get_bytes(url)
        if data is None:
            return None
        checksum = await client.get_bytes(url + ".CHECKSUM")
        if checksum is None:
            raise ChecksumError(f"no CHECKSUM file for {url}")
        expected = parse_checksum(checksum)
        actual = hashlib.sha256(data).hexdigest()
        if actual == expected:
            return data
        log.warning("checksum_mismatch", url=url, attempt=attempt)
    raise ChecksumError(f"checksum mismatch for {url}")


def read_zip_rows(data: bytes) -> list[list[str]]:
    """Rows of the single CSV inside a Binance zip, header row removed if present."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in zip, found {names}")
        text = zf.read(names[0]).decode("utf-8")
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if rows and not rows[0][0].strip().isdigit():
        rows = rows[1:]
    return rows


def rows_to_table(ds: Dataset, rows: list[list[str]]) -> pa.Table:
    if ds.kind == "funding":
        # calc_time, funding_interval_hours, last_funding_rate
        return funding_table(
            [int(r[0]) for r in rows],
            [float(r[2]) for r in rows],
            interval_hours=[int(r[1]) for r in rows],
        )
    return candles_table(ds, rows)


async def fetch_month(
    client: RateLimitedClient, symbol: str, ds: Dataset, year: int, month: int
) -> pa.Table | None:
    data = await fetch_verified_zip(client, monthly_url(symbol, ds, year, month))
    return None if data is None else rows_to_table(ds, read_zip_rows(data))


async def fetch_day(
    client: RateLimitedClient, symbol: str, ds: Dataset, day: date
) -> pa.Table | None:
    url = daily_url(symbol, ds, day)
    if url is None:
        return None
    data = await fetch_verified_zip(client, url)
    return None if data is None else rows_to_table(ds, read_zip_rows(data))
