"""Universe ranking: liquidity only.

Eligible: USDT-margined PERPETUAL contracts with status TRADING and a USDT-M symbol name.
Ranked by 24h quote volume (USDT, Decimal). The universe is the top `top_n` above the volume floor.
Current members only leave once they rank below `top_n + exit_rank_buffer` (or drop under the
floor / stop trading), so a symbol does not flip in and out on day-to-day noise.

Volatility, trend clarity and volume spikes are deliberately NOT used here: they belong to the
Market Scanner, which picks the day's opportunities inside this universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from deskmind.config import SYMBOL_RE
from deskmind.data.datasets import DAY_MS, to_ms
from deskmind.data.http_client import RateLimitedClient
from deskmind.universe.config import UniverseConfig

TICKER_24H_WEIGHT = 40  # /fapi/v1/ticker/24hr without a symbol


@dataclass(frozen=True)
class MarketSnapshot:
    exchange_info: dict[str, Any]
    tickers_24h: list[dict[str, Any]]


@dataclass(frozen=True)
class Candidate:
    symbol: str
    quote_volume_24h: Decimal
    rank: int  # 1 = most liquid eligible symbol
    onboard_at: datetime | None

    def listing_age_days(self, now: datetime) -> float | None:
        if self.onboard_at is None:
            return None
        return (to_ms(now) - to_ms(self.onboard_at)) / DAY_MS


@dataclass(frozen=True)
class Selection:
    members: dict[str, Candidate]  # the new universe
    ranked: dict[str, Candidate]  # every eligible symbol, for rank lookups


async def fetch_market_snapshot(client: RateLimitedClient) -> MarketSnapshot:
    """exchangeInfo + all 24h tickers from the public market-data API (no key, weight 41)."""
    info = await client.get_json("/fapi/v1/exchangeInfo", weight=1)
    tickers = await client.get_json("/fapi/v1/ticker/24hr", weight=TICKER_24H_WEIGHT)
    return MarketSnapshot(info, list(tickers))


def _eligible(sym: dict[str, Any]) -> bool:
    return (
        sym.get("contractType") == "PERPETUAL"
        and sym.get("status") == "TRADING"
        and sym.get("quoteAsset") == "USDT"
        and bool(SYMBOL_RE.match(str(sym.get("symbol", ""))))
    )


def _decimal(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def rank_by_liquidity(snapshot: MarketSnapshot) -> list[Candidate]:
    onboard: dict[str, datetime | None] = {}
    for sym in snapshot.exchange_info.get("symbols", []):
        if _eligible(sym):
            ms = sym.get("onboardDate")
            onboard[sym["symbol"]] = datetime.fromtimestamp(int(ms) / 1000, UTC) if ms else None
    volumes: list[tuple[str, Decimal]] = []
    for t in snapshot.tickers_24h:
        s = t.get("symbol")
        qv = _decimal(t.get("quoteVolume"))
        if s in onboard and qv is not None:
            volumes.append((s, qv))
    volumes.sort(key=lambda x: (-x[1], x[0]))
    return [Candidate(s, qv, i, onboard[s]) for i, (s, qv) in enumerate(volumes, 1)]


def select_universe(
    snapshot: MarketSnapshot, current_members: set[str], cfg: UniverseConfig
) -> Selection:
    ranked = {c.symbol: c for c in rank_by_liquidity(snapshot)}
    liquid = [c for c in ranked.values() if c.quote_volume_24h >= cfg.min_quote_volume_usdt]
    keep = {
        c.symbol: c
        for c in liquid
        if c.symbol in current_members and c.rank <= cfg.top_n + cfg.exit_rank_buffer
    }
    members = dict(keep)
    for c in liquid:
        if len(members) >= cfg.top_n:
            break
        members.setdefault(c.symbol, c)
    return Selection(members=members, ranked=ranked)
