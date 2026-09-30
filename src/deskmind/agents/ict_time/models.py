"""Items produced by the ICT Time agent. Times are epoch milliseconds (UTC).

Each item has a stable id, created_time (drawn), confirmed_at (knowable), and state.
`as_of(item, T)` gives the exact view of an item as of time T (docs/agents/ict_time.md §0).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

SessionName = Literal["asia", "london", "new_york"]
SessionState = Literal["active", "closed"]
LevelState = Literal["active", "swept", "taken"]
LevelType = Literal[
    "pdh", "pdl", "pwh", "pwl", "pmh", "pml",
    "daily_open", "weekly_open", "monthly_open",
    "ny_midnight_open", "asian_range_high", "asian_range_low",
]
KillzoneName = Literal["asian", "london_open", "ny_am", "ny_lunch", "london_close"]
SilverBulletName = Literal["sb1", "sb2", "sb3"]
AmdPhase = Literal["accumulation", "manipulation", "distribution", "unknown"]
AmdConfidence = Literal["low", "medium", "high"]


@dataclass
class SessionItem:
    """One trading session (Asia/London/NY) for one calendar date."""

    name: SessionName
    date_ny: str          # NY calendar date the session started (YYYY-MM-DD)
    created_time: int     # ms: open time of first bar of the session
    confirmed_at: int     # ms: bar close time when first candle of session closed
    state: SessionState = "active"
    high: float | None = None
    low: float | None = None
    open: float | None = None
    closed_at: int | None = None   # ms: when state -> closed
    swept_high_by: str | None = None   # sweep event id from ict_smc
    swept_low_by: str | None = None

    @property
    def id(self) -> str:
        return f"session_{self.name}_{self.date_ny}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": f"session_{self.name}",
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "name": self.name,
            "date_ny": self.date_ny,
            "state": self.state,
            "high": self.high,
            "low": self.low,
            "open": self.open,
            "closed_at": self.closed_at,
            "swept_high_by": self.swept_high_by,
            "swept_low_by": self.swept_low_by,
            "rule": "ICT session (spec §1)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "date_ny": self.date_ny,
            "state": self.state,
            "high": self.high,
            "low": self.low,
            "open": self.open,
            "swept_high_by": self.swept_high_by,
            "swept_low_by": self.swept_low_by,
        }


@dataclass
class KeyLevel:
    """Previous day/week/month high/low, opens, midnight open, Asian range."""

    level_type: LevelType
    price: float
    reference_period: str   # e.g. "2026-09-29" for PDH, "2026-W39" for PWH
    created_time: int       # ms: open time of bar that closed this level
    confirmed_at: int       # ms: close time of first bar of the new period
    state: LevelState = "active"
    swept_at: int | None = None
    taken_at: int | None = None
    swept_by: str | None = None   # ict_smc sweep event id

    @property
    def id(self) -> str:
        return f"{self.level_type}_{self.reference_period}"

    def item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.level_type,
            "created_time": self.created_time,
            "confirmed_at": self.confirmed_at,
            "level_type": self.level_type,
            "price": self.price,
            "reference_period": self.reference_period,
            "state": self.state,
            "swept_at": self.swept_at,
            "taken_at": self.taken_at,
            "swept_by": self.swept_by,
            "rule": "ICT key level (spec §4)",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level_type": self.level_type,
            "price": self.price,
            "state": self.state,
            "confirmed_at": self.confirmed_at,
            "swept_by": self.swept_by,
        }


def as_of(item: dict[str, Any], t: int) -> dict[str, Any] | None:
    """The item as it was known at epoch ms t, or None if not yet known."""
    if item["confirmed_at"] > t:
        return None
    view = copy.deepcopy(item)
    kind = view.get("kind", "")

    # Session state masking
    if kind.startswith("session_"):
        if view.get("closed_at") is not None and view["closed_at"] > t:
            view["closed_at"] = None
        view["state"] = "closed" if view.get("closed_at") is not None else "active"
        for field in ("swept_high_by", "swept_low_by"):
            pass  # sweeps tracked via separate items in ict_smc; no masking needed here

    # Key level state masking
    if kind in (
        "pdh", "pdl", "pwh", "pwl", "pmh", "pml",
        "daily_open", "weekly_open", "monthly_open",
        "ny_midnight_open", "asian_range_high", "asian_range_low",
    ):
        if view.get("taken_at") is not None and view["taken_at"] > t:
            view["taken_at"] = None
            view["swept_by"] = None
        if view.get("swept_at") is not None and view["swept_at"] > t:
            view["swept_at"] = None
            view["swept_by"] = None
        if view.get("taken_at") is not None:
            view["state"] = "taken"
        elif view.get("swept_at") is not None:
            view["state"] = "swept"
        else:
            view["state"] = "active"

    return view


def reconstruct_state(items: list[dict[str, Any]], t: int) -> dict[str, Any]:
    """Reconstruct state as of epoch ms T from item log (spec §0)."""
    views = [v for it in items if (v := as_of(it, t)) is not None]
    sessions = {v["name"]: v for v in views if v.get("kind", "").startswith("session_")}
    key_levels = {
        v["id"]: v
        for v in views
        if v.get("kind")
        in (
            "pdh", "pdl", "pwh", "pwl", "pmh", "pml",
            "daily_open", "weekly_open", "monthly_open",
            "ny_midnight_open", "asian_range_high", "asian_range_low",
        )
    }
    return {"sessions": sessions, "key_levels": key_levels}

