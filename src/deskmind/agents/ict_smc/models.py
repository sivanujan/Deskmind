"""Items produced by the ICT/SMC liquidity engine. Times are epoch milliseconds (UTC).

Each item has an `id` unique per (symbol, timeframe), a `kind`, `created_time` (where it is drawn),
`confirmed_at` (when it became knowable), and state tracking.
`as_of(item, T)` gives the exact view of an item as of time T (docs/agents/ict_smc.md §0).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

PoolSide = Literal["high", "low"]
PoolType = Literal["eqh", "eql", "single_high", "single_low"]
PoolState = Literal["active", "swept", "taken"]
Direction = Literal["bullish", "bearish"]
FvgState = Literal["fresh", "partially_filled", "filled", "inversion"]
ObState = Literal["fresh", "tested", "mitigated", "invalidated"]
BreakerState = Literal["active", "mitigated", "invalidated"]
VoidState = Literal["open", "rebalancing", "filled"]


@dataclass
class LiquidityPool:
    side: PoolSide  # "high" -> BSL, "low" -> SSL
    pool_type: PoolType  # "eqh", "eql", "single_high", "single_low"
    level: float
    swings: list[int]  # swing pivot times
    created_time: int
    confirmed_at: int
    state: PoolState = "active"
    swept_at: int | None = None
    swept_by: str | None = None
    taken_at: int | None = None
    taken_by: str | None = None
    expired_at: int | None = None

    @property
    def id(self) -> str:
        kind = "bsl" if self.side == "high" else "ssl"
        return f"{kind}_{self.pool_type}:{self.created_time}"

    @property
    def kind(self) -> str:
        return "bsl" if self.side == "high" else "ssl"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pivot_time": self.created_time,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "side": self.side,
            "pool_type": self.pool_type,
            "level": self.level,
            "swings": list(self.swings),
            "state": self.state,
            "swept_at": self.swept_at,
            "swept_by": self.swept_by,
            "taken_at": self.taken_at,
            "taken_by": self.taken_by,
            "expired_at": self.expired_at,
            "rule": "Equal swings within ATR tolerance -> BSL/SSL pool (spec §2)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "pool_type": self.pool_type,
            "state": self.state,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "swept_at": self.swept_at,
            "taken_at": self.taken_at,
        }


@dataclass
class Displacement:
    direction: Direction
    time: int
    confirmed_at: int
    open: float
    high: float
    low: float
    close: float
    size: float
    atr: float
    body_ratio: float

    @property
    def id(self) -> str:
        return f"displacement_{self.direction}:{self.time}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "displacement",
            "pivot_time": self.time,
            "created_time": self.time,
            "confirmed_at": self.confirmed_at,
            "direction": self.direction,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "size": self.size,
            "atr": self.atr,
            "body_ratio": self.body_ratio,
            "rule": "Candle range >= k*ATR with body ratio >= threshold (spec §3)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "time": self.time,
            "confirmed_at": self.confirmed_at,
            "size": self.size,
            "atr": self.atr,
        }


@dataclass
class FVG:
    direction: Direction
    top: float
    bottom: float
    ce: float  # Consequent Encroachment (50%)
    created_time: int
    confirmed_at: int
    state: FvgState = "fresh"
    partially_filled_at: int | None = None
    filled_at: int | None = None
    inverted_at: int | None = None
    expired_at: int | None = None

    @property
    def id(self) -> str:
        return f"fvg_{self.direction}:{self.created_time}"

    @property
    def kind(self) -> str:
        return f"fvg_{self.direction}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pivot_time": self.created_time,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "ce": self.ce,
            "state": self.state,
            "partially_filled_at": self.partially_filled_at,
            "filled_at": self.filled_at,
            "inverted_at": self.inverted_at,
            "expired_at": self.expired_at,
            "rule": "3-candle gap with displacement, CE at 50% (spec §4)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "ce": self.ce,
            "state": self.state,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class OrderBlock:
    direction: Direction  # "bullish" (demand) or "bearish" (supply)
    top: float
    bottom: float
    mt: float  # Mean Threshold (50%)
    created_time: int
    confirmed_at: int
    from_bos_id: str
    state: ObState = "fresh"
    tested_at: int | None = None
    mitigated_at: int | None = None
    invalidated_at: int | None = None
    expired_at: int | None = None

    @property
    def id(self) -> str:
        return f"ob_{self.direction}:{self.created_time}"

    @property
    def kind(self) -> str:
        return f"ob_{self.direction}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pivot_time": self.created_time,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "mt": self.mt,
            "from_bos_id": self.from_bos_id,
            "state": self.state,
            "tested_at": self.tested_at,
            "mitigated_at": self.mitigated_at,
            "invalidated_at": self.invalidated_at,
            "expired_at": self.expired_at,
            "rule": "Last opposite candle(s) before displacement causing BOS (spec §5)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "mt": self.mt,
            "state": self.state,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class BreakerBlock:
    direction: Direction  # new direction after flip
    is_mitigation: bool  # False = breaker (prior sweep), True = mitigation block
    top: float
    bottom: float
    created_time: int
    confirmed_at: int
    source_ob_id: str
    sweep_id: str | None = None
    state: BreakerState = "active"
    mitigated_at: int | None = None
    invalidated_at: int | None = None
    expired_at: int | None = None

    @property
    def id(self) -> str:
        prefix = "mitigation" if self.is_mitigation else "breaker"
        return f"{prefix}_{self.direction}:{self.created_time}"

    @property
    def kind(self) -> str:
        prefix = "mitigation_block" if self.is_mitigation else "breaker"
        return f"{prefix}_{self.direction}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pivot_time": self.created_time,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "direction": self.direction,
            "is_mitigation": self.is_mitigation,
            "top": self.top,
            "bottom": self.bottom,
            "source_ob_id": self.source_ob_id,
            "sweep_id": self.sweep_id,
            "state": self.state,
            "mitigated_at": self.mitigated_at,
            "invalidated_at": self.invalidated_at,
            "expired_at": self.expired_at,
            "rule": (
                "Mitigation block: OB failure without prior sweep (spec §5)"
                if self.is_mitigation
                else "Breaker block: OB invalidated after liquidity sweep (spec §5)"
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction,
            "is_mitigation": self.is_mitigation,
            "top": self.top,
            "bottom": self.bottom,
            "state": self.state,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class LiquidityVoid:
    direction: Direction
    top: float
    bottom: float
    created_time: int
    confirmed_at: int
    state: VoidState = "open"
    rebalanced_at: int | None = None
    filled_at: int | None = None
    expired_at: int | None = None

    @property
    def id(self) -> str:
        return f"void_{self.direction}:{self.created_time}"

    @property
    def kind(self) -> str:
        return "liquidity_void"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "pivot_time": self.created_time,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "state": self.state,
            "rebalanced_at": self.rebalanced_at,
            "filled_at": self.filled_at,
            "expired_at": self.expired_at,
            "rule": "Low-overlap multi-candle displacement run (spec §6)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "state": self.state,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class PDRange:
    low: float
    high: float
    from_bos_at: int
    equilibrium: float
    zone: Literal["discount", "premium", "equilibrium"]
    position_pct: float
    in_ote: bool
    ote_low: float
    ote_high: float
    confirmed_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "from_bos_at": self.from_bos_at,
            "equilibrium": self.equilibrium,
            "zone": self.zone,
            "position_pct": round(self.position_pct, 2),
            "in_ote": self.in_ote,
            "ote_low": self.ote_low,
            "ote_high": self.ote_high,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class SMTDivergence:
    pair: tuple[str, str]
    divergence_type: Literal["bullish", "bearish"]
    time: int
    confirmed_at: int
    symbol_a: str
    symbol_b: str
    note: str

    @property
    def id(self) -> str:
        return f"smt_{self.divergence_type}:{self.pair[0]}_{self.pair[1]}:{self.time}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": f"smt_{self.divergence_type}",
            "pivot_time": self.time,
            "created_time": self.time,
            "confirmed_at": self.confirmed_at,
            "pair": list(self.pair),
            "divergence_type": self.divergence_type,
            "symbol_a": self.symbol_a,
            "symbol_b": self.symbol_b,
            "note": self.note,
            "rule": "Aligned swing high/low divergence across correlated pairs (spec §8)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pair": f"{self.pair[0]}<->{self.pair[1]}",
            "type": self.divergence_type,
            "time": self.time,
            "confirmed_at": self.confirmed_at,
            "note": self.note,
        }


def as_of(item: dict[str, Any], t: int) -> dict[str, Any] | None:
    """The item as it was known at epoch ms t, or None if not known yet.

    Any state transition occurring after t is masked, reverting the item
    to its exact state as of time t.
    """
    if item["confirmed_at"] > t:
        return None
    view = copy.deepcopy(item)

    # Pool state masking
    if view.get("kind") in ("bsl", "ssl"):
        if view.get("taken_at") is not None and view["taken_at"] > t:
            view["taken_at"] = None
            view["taken_by"] = None
        if view.get("swept_at") is not None and view["swept_at"] > t:
            view["swept_at"] = None
            view["swept_by"] = None

        if view.get("taken_at") is not None:
            view["state"] = "taken"
        elif view.get("swept_at") is not None:
            view["state"] = "swept"
        else:
            view["state"] = "active"

    # FVG state masking
    elif "fvg" in view.get("kind", ""):
        if view.get("inverted_at") is not None and view["inverted_at"] > t:
            view["inverted_at"] = None
        if view.get("filled_at") is not None and view["filled_at"] > t:
            view["filled_at"] = None
        if view.get("partially_filled_at") is not None and view["partially_filled_at"] > t:
            view["partially_filled_at"] = None

        if view.get("inverted_at") is not None:
            view["state"] = "inversion"
        elif view.get("filled_at") is not None:
            view["state"] = "filled"
        elif view.get("partially_filled_at") is not None:
            view["state"] = "partially_filled"
        else:
            view["state"] = "fresh"

    # OB state masking
    elif "ob" in view.get("kind", ""):
        if view.get("invalidated_at") is not None and view["invalidated_at"] > t:
            view["invalidated_at"] = None
        if view.get("mitigated_at") is not None and view["mitigated_at"] > t:
            view["mitigated_at"] = None
        if view.get("tested_at") is not None and view["tested_at"] > t:
            view["tested_at"] = None

        if view.get("invalidated_at") is not None:
            view["state"] = "invalidated"
        elif view.get("mitigated_at") is not None:
            view["state"] = "mitigated"
        elif view.get("tested_at") is not None:
            view["state"] = "tested"
        else:
            view["state"] = "fresh"

    # Breaker state masking
    elif "breaker" in view.get("kind", "") or "mitigation_block" in view.get("kind", ""):
        if view.get("invalidated_at") is not None and view["invalidated_at"] > t:
            view["invalidated_at"] = None
        if view.get("mitigated_at") is not None and view["mitigated_at"] > t:
            view["mitigated_at"] = None

        if view.get("invalidated_at") is not None:
            view["state"] = "invalidated"
        elif view.get("mitigated_at") is not None:
            view["state"] = "mitigated"
        else:
            view["state"] = "active"

    # Void state masking
    elif view.get("kind") == "liquidity_void":
        if view.get("filled_at") is not None and view["filled_at"] > t:
            view["filled_at"] = None
        if view.get("rebalanced_at") is not None and view["rebalanced_at"] > t:
            view["rebalanced_at"] = None

        if view.get("filled_at") is not None:
            view["state"] = "filled"
        elif view.get("rebalanced_at") is not None:
            view["state"] = "rebalancing"
        else:
            view["state"] = "open"

    # Expired_at masking
    if view.get("expired_at") is not None and view["expired_at"] > t:
        view["expired_at"] = None

    return view
