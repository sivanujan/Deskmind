"""Live check against Binance public data. Skipped by default; run: uv run pytest -m network"""

from datetime import UTC, date, datetime

import pytest

from deskmind.data import bulk_download as bulk
from deskmind.data import rest_backfill as rest
from deskmind.data.datasets import DAY_MS, Dataset, to_ms
from deskmind.data.pipeline import make_clients

pytestmark = pytest.mark.network


async def test_bulk_day_equals_rest_day() -> None:
    ds = Dataset("klines", "1h")
    day = date(2024, 3, 1)
    start = to_ms(datetime(2024, 3, 1, tzinfo=UTC))
    fapi, vision = make_clients("https://fapi.binance.com")
    async with fapi, vision:
        from_bulk = await bulk.fetch_day(vision, "BTCUSDT", ds, day)
        from_rest = await rest.fetch_candles_range(fapi, "BTCUSDT", ds, start, start + DAY_MS)
    assert from_bulk is not None and from_bulk.num_rows == 24
    cols = ["open_time", "open", "high", "low", "close", "volume"]
    assert from_bulk.select(cols).equals(from_rest.select(cols))
