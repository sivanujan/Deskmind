"""Redis layout for live market data + read/write helpers (reusable by agents).

Schema version 1. All keys are prefixed `deskmind:v1:`; symbols are upper-case, timeframes as
Binance writes them (5m, 15m, 1h, 2h). Times are epoch milliseconds (UTC).

    deskmind:schema_version                     STRING "1"
    deskmind:v1:candles:{SYM}:{tf}              ZSET   score=open_time, member=compact kline
                                                       (last BUFFER_SIZE closed candles only)
    deskmind:v1:open:{SYM}:{tf}                 STRING JSON {"is_open": true, "k": [...],
                                                       "event_time", "updated_at"}  (display only)
    deskmind:v1:mark:{SYM}                      STRING JSON {mark_price, index_price,
                                                       est_settle_price, funding_rate,
                                                       next_funding_time, event_time, received_at}
    deskmind:v1:oi:{SYM}                        STRING JSON {open_interest, time, received_at}
    deskmind:v1:health:streams                  HASH   stream name -> last message time
    deskmind:v1:health:service                  HASH   state, ws_state, heartbeat, reconnects, ...
    deskmind:v1:bar_close:last:{SYM}            STRING last emitted close_time
    deskmind:v1:bar_close:sent:{SYM}:{close}    STRING dedupe marker (SET NX, 7 day TTL)
    deskmind:v1:bar_close:log                   STREAM every emitted event (capped)
    deskmind:v1:control:ws_block_until          STRING test hook: WebSocket stays down until then
    channel deskmind:bar_close                  PUB/SUB bar-close events

Compact kline (JSON array, Binance's own strings so values stay exact as Decimal):
    [open_time, open, high, low, close, volume, close_time, quote_volume, trades,
     taker_buy_volume, taker_buy_quote_volume]
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

SCHEMA_VERSION = 1
PREFIX = f"deskmind:v{SCHEMA_VERSION}"
BUFFER_SIZE = 500
BAR_CLOSE_CHANNEL = "deskmind:bar_close"
EVENT_LOG_MAXLEN = 20_000
SENT_TTL_S = 7 * 24 * 3600


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------- keys


def candles_key(symbol: str, tf: str) -> str:
    return f"{PREFIX}:candles:{symbol}:{tf}"


def open_key(symbol: str, tf: str) -> str:
    return f"{PREFIX}:open:{symbol}:{tf}"


def mark_key(symbol: str) -> str:
    return f"{PREFIX}:mark:{symbol}"


def oi_key(symbol: str) -> str:
    return f"{PREFIX}:oi:{symbol}"


HEALTH_STREAMS = f"{PREFIX}:health:streams"
HEALTH_SERVICE = f"{PREFIX}:health:service"
EVENT_LOG = f"{PREFIX}:bar_close:log"
WS_BLOCK_UNTIL = f"{PREFIX}:control:ws_block_until"


def last_emitted_key(symbol: str) -> str:
    return f"{PREFIX}:bar_close:last:{symbol}"


def sent_key(symbol: str, close_time: int) -> str:
    return f"{PREFIX}:bar_close:sent:{symbol}:{close_time}"


# ---------------------------------------------------------------- model


@dataclass(frozen=True)
class Kline:
    """One candle with exact Decimal values. `is_closed` False = still forming."""

    symbol: str
    timeframe: str
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int
    taker_buy_volume: Decimal
    taker_buy_quote_volume: Decimal
    is_closed: bool = True

    @classmethod
    def from_ws(cls, k: dict[str, Any], symbol: str | None = None) -> Kline:
        """From a WebSocket kline payload's `k` object."""
        return cls(
            symbol=(symbol or k["s"]).upper(),
            timeframe=k["i"],
            open_time=int(k["t"]),
            close_time=int(k["T"]),
            open=Decimal(k["o"]),
            high=Decimal(k["h"]),
            low=Decimal(k["l"]),
            close=Decimal(k["c"]),
            volume=Decimal(k["v"]),
            quote_volume=Decimal(k["q"]),
            trades=int(k["n"]),
            taker_buy_volume=Decimal(k["V"]),
            taker_buy_quote_volume=Decimal(k["Q"]),
            is_closed=bool(k["x"]),
        )

    @classmethod
    def from_rest(cls, row: list[Any], symbol: str, tf: str, closed: bool = True) -> Kline:
        """From a REST /fapi/v1/klines row."""
        return cls.from_compact(symbol, tf, row[:11], closed)

    @classmethod
    def from_compact(cls, symbol: str, tf: str, a: list[Any], closed: bool = True) -> Kline:
        return cls(
            symbol=symbol,
            timeframe=tf,
            open_time=int(a[0]),
            open=Decimal(str(a[1])),
            high=Decimal(str(a[2])),
            low=Decimal(str(a[3])),
            close=Decimal(str(a[4])),
            volume=Decimal(str(a[5])),
            close_time=int(a[6]),
            quote_volume=Decimal(str(a[7])),
            trades=int(a[8]),
            taker_buy_volume=Decimal(str(a[9])),
            taker_buy_quote_volume=Decimal(str(a[10])),
            is_closed=closed,
        )

    def compact(self) -> list[Any]:
        return [
            self.open_time,
            str(self.open),
            str(self.high),
            str(self.low),
            str(self.close),
            str(self.volume),
            self.close_time,
            str(self.quote_volume),
            self.trades,
            str(self.taker_buy_volume),
            str(self.taker_buy_quote_volume),
        ]

    def member(self) -> str:
        return json.dumps(self.compact(), separators=(",", ":"))

    def same_values(self, other: Kline) -> bool:
        return self.compact() == other.compact()


# ---------------------------------------------------------------- writes


async def put_closed(redis: Redis, klines: list[Kline], size: int = BUFFER_SIZE) -> None:
    """Upsert closed candles into their rolling buffers (idempotent, capped at `size`)."""
    if not klines:
        return
    pipe = redis.pipeline(transaction=True)
    touched: set[str] = set()
    for k in klines:
        key = candles_key(k.symbol, k.timeframe)
        pipe.zremrangebyscore(key, k.open_time, k.open_time)  # replace, never duplicate
        pipe.zadd(key, {k.member(): k.open_time})
        touched.add(key)
    for key in touched:
        pipe.zremrangebyrank(key, 0, -(size + 1))
    await pipe.execute()


async def put_open(redis: Redis, k: Kline, event_time: int) -> bool:
    """Store the forming candle for display. Ignores updates older than what is stored."""
    key = open_key(k.symbol, k.timeframe)
    current = await redis.get(key)
    if current:
        cur = json.loads(current)
        if (cur["k"][0], cur["event_time"]) > (k.open_time, event_time):
            return False
    doc = {"is_open": True, "k": k.compact(), "event_time": event_time, "updated_at": now_ms()}
    await redis.set(key, json.dumps(doc, separators=(",", ":")))
    return True


async def put_json_if_newer(redis: Redis, key: str, doc: dict[str, Any], order_field: str) -> bool:
    """Store doc unless the stored one has a newer `order_field` (out-of-order safe)."""
    current = await redis.get(key)
    if current and json.loads(current).get(order_field, 0) > doc[order_field]:
        return False
    await redis.set(key, json.dumps(doc, separators=(",", ":")))
    return True


# ---------------------------------------------------------------- reads (for agents)


async def get_candles(redis: Redis, symbol: str, tf: str, n: int = BUFFER_SIZE) -> list[Kline]:
    """Last `n` CLOSED candles, oldest first. Never includes the forming candle."""
    members = await redis.zrange(candles_key(symbol, tf), -n, -1)
    return [Kline.from_compact(symbol, tf, json.loads(m)) for m in members]


async def get_last_closed(redis: Redis, symbol: str, tf: str) -> Kline | None:
    c = await get_candles(redis, symbol, tf, 1)
    return c[0] if c else None


async def get_open_candle(redis: Redis, symbol: str, tf: str) -> dict[str, Any] | None:
    raw = await redis.get(open_key(symbol, tf))
    return json.loads(raw) if raw else None


async def get_mark(redis: Redis, symbol: str) -> dict[str, Any] | None:
    raw = await redis.get(mark_key(symbol))
    return json.loads(raw) if raw else None


async def get_open_interest(redis: Redis, symbol: str) -> dict[str, Any] | None:
    raw = await redis.get(oi_key(symbol))
    return json.loads(raw) if raw else None


async def buffer_size(redis: Redis, symbol: str, tf: str) -> int:
    return int(await redis.zcard(candles_key(symbol, tf)))
