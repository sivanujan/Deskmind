"""Dataset definitions and UTC time helpers shared by the data pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS

TIMEFRAME_MS: dict[str, int] = {
    "1m": MINUTE_MS,
    "3m": 3 * MINUTE_MS,
    "5m": 5 * MINUTE_MS,
    "15m": 15 * MINUTE_MS,
    "30m": 30 * MINUTE_MS,
    "1h": HOUR_MS,
    "2h": 2 * HOUR_MS,
    "4h": 4 * HOUR_MS,
    "1d": DAY_MS,
}

Kind = Literal["klines", "mark_klines", "funding"]


@dataclass(frozen=True)
class Dataset:
    kind: Kind
    timeframe: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.timeframe}" if self.timeframe else self.kind

    @property
    def is_candles(self) -> bool:
        return self.kind in ("klines", "mark_klines")

    @property
    def interval_ms(self) -> int:
        if self.timeframe is None:
            raise ValueError(f"{self.key} has no fixed candle interval")
        return TIMEFRAME_MS[self.timeframe]

    @property
    def bulk_folder(self) -> str:
        return {"klines": "klines", "mark_klines": "markPriceKlines", "funding": "fundingRate"}[
            self.kind
        ]

    @property
    def rest_path(self) -> str:
        return {
            "klines": "/fapi/v1/klines",
            "mark_klines": "/fapi/v1/markPriceKlines",
            "funding": "/fapi/v1/fundingRate",
        }[self.kind]


KLINES = tuple(Dataset("klines", tf) for tf in ("5m", "15m", "1h", "2h"))
MARK_KLINES = tuple(Dataset("mark_klines", tf) for tf in ("5m", "1h"))
FUNDING = Dataset("funding")
DEFAULT_DATASETS: tuple[Dataset, ...] = (*KLINES, *MARK_KLINES, FUNDING)

# (source 5m dataset, target) pairs checked for resample consistency.
CROSS_TF_CHECKS: tuple[tuple[Dataset, Dataset], ...] = (
    (Dataset("klines", "5m"), Dataset("klines", "15m")),
    (Dataset("klines", "5m"), Dataset("klines", "1h")),
    (Dataset("klines", "5m"), Dataset("klines", "2h")),
    (Dataset("mark_klines", "5m"), Dataset("mark_klines", "1h")),
)

# (last-price klines, mark-price klines) pairs compared for abnormal divergence (report only).
MARK_LAST_CHECKS: tuple[tuple[Dataset, Dataset], ...] = (
    (Dataset("klines", "5m"), Dataset("mark_klines", "5m")),
    (Dataset("klines", "1h"), Dataset("mark_klines", "1h")),
)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all times must be UTC-aware")
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def date_ms(d: date) -> int:
    return to_ms(datetime(d.year, d.month, d.day, tzinfo=UTC))


def month_bounds_ms(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return to_ms(start), to_ms(end)


def iter_months(start: date, end: date) -> Iterator[tuple[int, int]]:
    """(year, month) from start's month through end's month, inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def iter_days(start_ms: int, end_ms: int) -> Iterator[date]:
    """UTC dates whose midnight lies in [start_ms, end_ms)."""
    d = from_ms(start_ms).date()
    while date_ms(d) < end_ms:
        yield d
        d += timedelta(days=1)


def last_closed_open_ms(now_ms: int, interval_ms: int) -> int:
    """Exclusive upper bound for open_time of closed candles at now_ms.

    A candle opening at t is closed once t + interval <= now, which for aligned t is exactly
    t < floor(now / interval) * interval. The currently forming candle is therefore excluded.
    """
    return (now_ms // interval_ms) * interval_ms


def normalize_funding_ms(ms: int) -> int:
    """Binance stamps funding events a few ms after the hour; round to the nearest second."""
    return (ms + 500) // 1000 * 1000
