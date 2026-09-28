"""Universe lifecycle states and records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# watch_only  : listed < MIN_LISTING_AGE days; data collected, chart only, never traded
# downloading : history for the needed period + warm-up is being fetched
# verifying   : `data verify` checks are running
# warming_up  : analyst agents are replaying recent history
# tradeable   : data verified AND agent warm-up completed (trading also needs TRADE_SYMBOLS)
# blocked     : download or verification failed (or disk guard); retried by the next daily run
# exited      : left the universe; no new entries, data kept
State = Literal[
    "watch_only", "downloading", "verifying", "warming_up", "tradeable", "blocked", "exited"
]
STATES: tuple[str, ...] = (
    "watch_only",
    "downloading",
    "verifying",
    "warming_up",
    "tradeable",
    "blocked",
    "exited",
)
# States whose data passed verification (chartable).
VERIFIED_STATES = frozenset({"watch_only", "warming_up", "tradeable", "exited"})


@dataclass(frozen=True)
class SymbolStat:
    """Liquidity input for one symbol. Deliberately has no volatility/trend fields: the universe
    is liquidity only; the (later) Market Scanner picks opportunities inside it."""

    symbol: str
    onboard_ms: int  # Binance onboardDate (listing time)
    median_quote_volume: float  # median daily quote volume (USDT) over the lookback, closed days
    days: int  # closed daily candles used


@dataclass
class SymbolRecord:
    symbol: str
    state: str
    rank: int | None = None
    median_quote_volume: float | None = None
    onboard_ms: int | None = None
    member: bool = True
    data_verified: bool = False
    warmed_up: bool = False
    reason: str | None = None  # why blocked / why not tradeable
    entered_at_ms: int | None = None
    exited_at_ms: int | None = None
    state_changed_at_ms: int | None = None
    history: list[dict[str, Any]] = field(default_factory=list)  # last state transitions

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RankResult:
    members: list[str]  # after the exit buffer, best rank first
    entered: list[str]
    exited: list[str]
    ranks: dict[str, int]
