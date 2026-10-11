"""Symbol lifecycle states and the trading gate.

watch_only  listing younger than min_listing_age_days: data is collected, chart only
downloading history (new symbol) or an update (returning / promoted symbol)
verifying   `data verify` checks on the stored history
warming_up  the analyst agents replay history before their first live report
tradeable   verified data + completed warm-up: new entries allowed
exited      left the universe: no new entries, data kept
blocked     a step failed (download, verification, warm-up, disk guard); an alert was raised
            and the symbol is retried by the next daily run. Never tradeable while blocked.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any


class SymbolState(enum.StrEnum):
    watch_only = "watch_only"
    downloading = "downloading"
    verifying = "verifying"
    warming_up = "warming_up"
    tradeable = "tradeable"
    exited = "exited"
    blocked = "blocked"


class BlockedReason(enum.StrEnum):
    download_failed = "download_failed"
    verification_failed = "verification_failed"
    warmup_failed = "warmup_failed"
    disk_guard = "disk_guard"


# States of a symbol that is still in the middle of (or failed) the entry pipeline.
PIPELINE_STATES = frozenset(
    {
        SymbolState.downloading,
        SymbolState.verifying,
        SymbolState.warming_up,
        SymbolState.blocked,
    }
)


@dataclass
class SymbolRecord:
    symbol: str
    state: SymbolState
    in_universe: bool = True
    rank: int | None = None
    quote_volume_24h: Decimal | None = None
    onboard_at: datetime | None = None
    entered_at: datetime | None = None
    exited_at: datetime | None = None
    state_changed_at: datetime | None = None
    verified_at: datetime | None = None
    warmed_up_at: datetime | None = None
    blocked_reason: BlockedReason | None = None
    last_error: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)  # recent state transitions

    def transition(self, state: SymbolState, now: datetime, **changes: Any) -> SymbolRecord:
        rec = replace(self, **changes)
        if state != self.state:
            rec.history = [
                *self.history[-19:],
                {"at": now.isoformat(), "from": str(self.state), "to": str(state)},
            ]
            rec.state_changed_at = now
        rec.state = state
        if state != SymbolState.blocked:
            rec.blocked_reason = None
        return rec


def is_tradeable(rec: SymbolRecord | None) -> bool:
    """Tradeable ONLY after its data passed verification and agent warm-up completed."""
    return (
        rec is not None
        and rec.in_universe
        and rec.state == SymbolState.tradeable
        and rec.verified_at is not None
        and rec.warmed_up_at is not None
        and rec.warmed_up_at >= rec.verified_at
    )


def allows_new_entries(rec: SymbolRecord | None) -> bool:
    """New positions may only be opened on tradeable symbols (never watch_only/exited/blocked)."""
    return is_tradeable(rec)


def chart_visible(rec: SymbolRecord | None) -> bool:
    """Every symbol with stored data can be charted, including watch_only and exited ones."""
    return rec is not None
