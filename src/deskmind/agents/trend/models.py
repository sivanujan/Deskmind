"""Items produced by the trend engine. Times are epoch milliseconds (UTC).

Each item has an `id` unique per (symbol, timeframe), a `kind`, `pivot_time` (where it is drawn),
`confirmed_at` (when it became knowable) and, for items that change later, the time of each
change. `as_of(item, T)` gives the view of an item at time T (see docs/agents/trend.md §0).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Literal

Side = Literal["high", "low"]
Direction = Literal["up", "down"]


@dataclass(frozen=True)
class Bar:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class Swing:
    side: Side
    price: float
    pivot_time: int
    confirmed_at: int
    label: str | None  # HH / LH / HL / LL, None for the first swing of a side
    idx: int  # engine candle index of the pivot
    atr: float
    broken_at: int | None = None

    @property
    def id(self) -> str:
        return f"swing_{self.side}:{self.pivot_time}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": f"swing_{self.side}",
            "pivot_time": self.pivot_time,
            "confirmed_at": self.confirmed_at,
            "price": self.price,
            "label": self.label,
            "atr": self.atr,
            "broken_at": self.broken_at,
            "rule": "fractal pivot + ATR size filter (spec §2-3)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "label": self.label,
            "pivot_time": self.pivot_time,
            "confirmed_at": self.confirmed_at,
            "broken_at": self.broken_at,
        }


@dataclass
class Break:
    type: Literal["BOS", "CHoCH"]
    direction: Direction
    level: float
    time: int  # break candle open (drawing)
    confirmed_at: int  # break candle close
    swing_pivot_time: int
    close: float

    @property
    def id(self) -> str:
        return f"{self.type.lower()}_{self.direction}:{self.time}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.type.lower(),
            "pivot_time": self.time,
            "confirmed_at": self.confirmed_at,
            "type": self.type,
            "direction": self.direction,
            "level": self.level,
            "close": self.close,
            "swing_pivot_time": self.swing_pivot_time,
            "rule": f"candle close {'above' if self.direction == 'up' else 'below'} the last "
            f"unbroken swing {'high' if self.direction == 'up' else 'low'} (spec §4)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "direction": self.direction,
            "level": self.level,
            "time": self.time,
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class Sweep:
    direction: Direction
    level: float
    extreme: float  # wick high/low
    close: float
    time: int
    confirmed_at: int
    swing_pivot_time: int

    @property
    def id(self) -> str:
        return f"sweep_{self.direction}:{self.time}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "sweep",
            "pivot_time": self.time,
            "confirmed_at": self.confirmed_at,
            "direction": self.direction,
            "level": self.level,
            "extreme": self.extreme,
            "close": self.close,
            "swing_pivot_time": self.swing_pivot_time,
            "rule": "wick beyond the swing level, close back inside (spec §4)",
        }


@dataclass
class Touch:
    pivot_time: int
    price: float
    confirmed_at: int


@dataclass
class TrendLine:
    side: Literal["support", "resistance"]
    a_time: int
    a_price: float
    b_time: int
    b_price: float
    tol: float
    confirmed_at: int
    touches: list[Touch] = field(default_factory=list)
    broken_at: int | None = None
    broken_time: int | None = None  # open time of the breaking candle
    expired_at: int | None = None
    last_touch_idx: int = 0  # engine candle index of the newest touch (not part of the item)

    @property
    def slope(self) -> float:
        return (self.b_price - self.a_price) / (self.b_time - self.a_time)

    def value(self, t: int) -> float:
        return self.a_price + self.slope * (t - self.a_time)

    @property
    def id(self) -> str:
        return f"trendline_{self.side}:{self.a_time}:{self.b_time}"

    @property
    def last_touch(self) -> int:
        return max(t.pivot_time for t in self.touches)

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": f"trendline_{self.side}",
            "pivot_time": self.a_time,
            "confirmed_at": self.confirmed_at,
            "a_time": self.a_time,
            "a_price": self.a_price,
            "b_time": self.b_time,
            "b_price": self.b_price,
            "slope_per_ms": self.slope,
            "tol": self.tol,
            "touches": [t.__dict__ for t in self.touches],
            "broken_at": self.broken_at,
            "broken_time": self.broken_time,
            "expired_at": self.expired_at,
            "rule": ">= 3 swing touches within tolerance, no close through (spec §6)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "a_time": self.a_time,
            "a_price": self.a_price,
            "slope_per_hour": self.slope * 3_600_000,
            "touches": len(self.touches),
            "last_touch": self.last_touch,
            "confirmed_at": self.confirmed_at,
        }


def as_of(item: dict[str, Any], t: int) -> dict[str, Any] | None:
    """The item as it was known at time t, or None if it was not known yet."""
    if item["confirmed_at"] > t:
        return None
    view = copy.deepcopy(item)
    for key in ("broken_at", "expired_at"):
        if view.get(key) is not None and view[key] > t:
            view[key] = None
            if key == "broken_at" and "broken_time" in view:
                view["broken_time"] = None
    if "touches" in view:
        view["touches"] = [x for x in view["touches"] if x["confirmed_at"] <= t]
    return view
