"""Point-in-time universe history (`universe_membership`, `universe_delistings`).

- Row (day D, symbol) = symbol was a universe member on day D, decided ONLY from daily candles
  that closed before D (same rule as the live 00:01 UTC job). `tradeable` = also listed for at
  least `min_listing_age_days` on D (otherwise it was watch_only that day).
- Backtests call `MembershipHistory.get_universe(D)`: a symbol can only be entered on a day it
  was a tradeable member. A missing day means "no members", never "everything".
- History is rebuilt from data.binance.vision daily klines, which still hold DELISTED symbols, so
  they appear in history on the days they were liquid, while today's universe (exchangeInfo,
  status TRADING) no longer contains them. Every zip is checked against its .CHECKSUM.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from deskmind.logging import get_logger
from deskmind.universe.models import SymbolStat
from deskmind.universe.ranking import DAY_MS, UniverseConfig, rank_universe

log = get_logger(__name__)

VISION_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
KLINES_PREFIX = "data/futures/um/monthly/klines/"
USDT_PERP_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")  # excludes dated contracts like X_240628


@dataclass(frozen=True)
class MembershipRow:
    day: date
    symbol: str
    rank: int
    median_quote_volume: float
    tradeable: bool  # member AND listed >= min_listing_age_days on `day`


def day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def ms_day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).date()


class MembershipHistory:
    """Preloaded membership for a date range (the backtest engine is synchronous)."""

    def __init__(
        self, rows: Iterable[MembershipRow], delisted_at_ms: dict[str, int] | None = None
    ) -> None:
        self._by_day: dict[date, dict[str, MembershipRow]] = {}
        for r in rows:
            self._by_day.setdefault(r.day, {})[r.symbol] = r
        self.delisted_at_ms = dict(delisted_at_ms or {})

    def get_universe(self, as_of_date: date, include_watch_only: bool = False) -> set[str]:
        """Members on `as_of_date` (tradeable only unless include_watch_only)."""
        rows = self._by_day.get(as_of_date, {})
        return {s for s, r in rows.items() if include_watch_only or r.tradeable}

    def days(self) -> list[date]:
        return sorted(self._by_day)

    def covers(self, start: date, end: date) -> bool:
        d = start
        while d <= end:
            if d not in self._by_day:
                return False
            d += timedelta(days=1)
        return True

    def missing_days(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d <= end:
            if d not in self._by_day:
                out.append(d)
            d += timedelta(days=1)
        return out


def compute_membership(
    daily_volume: dict[str, dict[date, float]],
    start: date,
    end: date,
    cfg: UniverseConfig,
    first_day: dict[str, date] | None = None,
) -> list[MembershipRow]:
    """Replay the daily ranking for every day in [start, end] with no look-ahead.

    daily_volume[symbol][d] = quote volume of the daily candle opening on d. For day D only
    candles of days < D are used (the candle of D itself has not closed at 00:01 UTC). A symbol
    whose data stopped (delisted) drops out once it has no candle on D-1.
    """
    first = first_day or {s: min(v) for s, v in daily_volume.items() if v}
    rows: list[MembershipRow] = []
    prev: set[str] = set()
    d = start
    while d <= end:
        window = [d - timedelta(days=i) for i in range(cfg.lookback_days, 0, -1)]
        yesterday = d - timedelta(days=1)
        stats = []
        for sym, vols in daily_volume.items():
            if yesterday not in vols:  # not trading any more (or not yet listed)
                continue
            w = [vols[x] for x in window if x in vols]
            listed = first.get(sym, yesterday)
            stats.append(SymbolStat(sym, day_ms(listed), statistics.median(w), len(w)))
        res = rank_universe(stats, prev, cfg)
        by = {s.symbol: s for s in stats}
        for sym in res.members:
            age_days = (d - ms_day(by[sym].onboard_ms)).days
            rows.append(
                MembershipRow(
                    d,
                    sym,
                    res.ranks[sym],
                    by[sym].median_quote_volume,
                    age_days >= cfg.min_listing_age_days,
                )
            )
        prev = set(res.members)
        d += timedelta(days=1)
    return rows


def find_delistings(
    daily_volume: dict[str, dict[date, float]], trading_now: set[str], today: date
) -> dict[str, int]:
    """Symbols with history that are no longer TRADING: delisted at the close of their last
    daily candle."""
    out = {}
    for sym, vols in daily_volume.items():
        if sym in trading_now or not vols:
            continue
        last = max(vols)
        if last < today - timedelta(days=1):
            out[sym] = day_ms(last) + DAY_MS
    return out


# ---------------------------------------------------------------- data.binance.vision


def parse_s3_listing(xml: str) -> tuple[list[str], str | None]:
    """(USDT perpetual symbols, next marker) from one S3 ListBucket page."""
    prefixes = re.findall(r"<Prefix>" + re.escape(KLINES_PREFIX) + r"([^/<]+)/</Prefix>", xml)
    symbols = [p for p in prefixes if USDT_PERP_RE.match(p)]
    truncated = "<IsTruncated>true</IsTruncated>" in xml
    marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", xml)
    next_marker = marker.group(1) if truncated and marker else None
    if truncated and next_marker is None and prefixes:
        next_marker = KLINES_PREFIX + prefixes[-1] + "/"
    return symbols, next_marker


async def list_vision_symbols(client: Any) -> list[str]:
    """Every USDT perpetual that ever had klines on data.binance.vision (incl. delisted)."""
    symbols: list[str] = []
    marker: str | None = None
    while True:
        url = f"{VISION_LIST_URL}?delimiter=/&prefix={KLINES_PREFIX}"
        if marker:
            url += f"&marker={marker}"
        raw = await client.get_bytes(url)
        page, marker = parse_s3_listing((raw or b"").decode("utf-8"))
        symbols.extend(page)
        if not marker:
            return sorted(set(symbols))


def _daily_rows(zip_bytes: bytes) -> dict[date, float]:
    from deskmind.data.bulk_download import read_zip_rows

    return {ms_day(int(r[0])): float(r[7]) for r in read_zip_rows(zip_bytes)}


async def fetch_daily_volumes(
    client: Any, symbol: str, start: date, end: date
) -> dict[date, float]:
    """Daily quote volume from verified monthly (then daily) 1d kline zips."""
    from deskmind.data.bulk_download import fetch_verified_zip

    out: dict[date, float] = {}
    base = "/data/futures/um"
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        url = f"{base}/monthly/klines/{symbol}/1d/{symbol}-1d-{y:04d}-{m:02d}.zip"
        data = await fetch_verified_zip(client, url)
        if data is not None:
            out.update(_daily_rows(data))
        elif (y, m) == (end.year, end.month):  # current month: daily files
            d = date(y, m, 1)
            while d <= end:
                url = f"{base}/daily/klines/{symbol}/1d/{symbol}-1d-{d.isoformat()}.zip"
                day_zip = await fetch_verified_zip(client, url)
                if day_zip is not None:
                    out.update(_daily_rows(day_zip))
                d += timedelta(days=1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {d: v for d, v in out.items() if start <= d <= end}


async def build_history(
    vision: Any,
    fapi: Any,
    start: date,
    end: date,
    cfg: UniverseConfig,
) -> tuple[list[MembershipRow], dict[str, int]]:
    """Rebuild membership for [start, end] from data.binance.vision (all symbols, incl.
    delisted) and today's exchangeInfo (to know which are still trading)."""
    symbols = await list_vision_symbols(vision)
    # Far enough back to know the listing age at `start` (watch_only vs tradeable).
    fetch_from = start - timedelta(days=cfg.min_listing_age_days + cfg.lookback_days + 1)
    volumes: dict[str, dict[date, float]] = {}
    sem = asyncio.Semaphore(16)
    done_count = 0

    async def _fetch_one(s: str) -> tuple[str, dict[date, float]]:
        nonlocal done_count
        async with sem:
            v = await fetch_daily_volumes(vision, s, fetch_from, end)
            done_count += 1
            if done_count % 50 == 0 or done_count == len(symbols):
                log.info("universe_history_progress", done=done_count, total=len(symbols))
            return s, v

    batch_results = await asyncio.gather(*[_fetch_one(s) for s in symbols])
    for s, v in batch_results:
        volumes[s] = v
    info = await fapi.get_json("/fapi/v1/exchangeInfo", weight=1)
    trading = {
        s["symbol"]
        for s in info.get("symbols", [])
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
    }
    onboard = {s["symbol"]: int(s.get("onboardDate") or 0) for s in info.get("symbols", [])}
    first_day: dict[str, date] = {}
    for s, v in volumes.items():
        if not v:
            continue
        if onboard.get(s):  # exact listing time for symbols Binance still reports
            first_day[s] = ms_day(onboard[s])
        elif min(v) <= fetch_from + timedelta(days=1):  # data from the window start: old listing
            first_day[s] = date(2000, 1, 1)
        else:
            first_day[s] = min(v)
    rows = compute_membership(volumes, start, end, cfg, first_day)
    return rows, find_delistings(volumes, trading, end + timedelta(days=1))
