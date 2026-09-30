"""Items produced by the Price Action agent. All times are epoch ms (UTC).

`as_of(item, T)` returns the item as it was known at time T, or None if not yet
confirmed. State fields that changed after T are masked back to their state at T.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

ZoneState = Literal["active", "broken", "flipped", "retired"]
PatternKind = Literal[
    "bullish_engulfing", "bearish_engulfing",
    "hammer", "shooting_star",
    "inside_bar", "outside_bar",
    "doji",
    "morning_star", "evening_star",
]
BreakoutOutcome = Literal["held", "failed"] | None
PatternContext = Literal["at_zone", "open_space"]
RangeState = Literal["active", "broken"]


def as_of(item: dict[str, Any], t: int) -> dict[str, Any] | None:
    """Item view as of epoch ms t, or None if not yet confirmed."""
    if item.get("confirmed_at", 0) > t:
        return None
    view = copy.deepcopy(item)
    kind = view.get("kind", "")

    # --- S/R zone state masking
    if kind in ("sr_support", "sr_resistance"):
        if view.get("broken_at") and view["broken_at"] > t:
            view["broken_at"] = None
            view["flipped_at"] = None
            view["retired_at"] = None
            view["state"] = "active"
        elif view.get("flipped_at") and view["flipped_at"] > t:
            view["flipped_at"] = None
            view["retired_at"] = None
            view["state"] = "broken"
        elif view.get("retired_at") and view["retired_at"] > t:
            view["retired_at"] = None
            view["state"] = _sr_state_before_retired(view, t)
        # Recompute touches up to t
        if "touches" in view:
            view["touches"] = [tc for tc in view["touches"] if tc <= t]
        # Mask strength to what was computable at t (simplified: use stored strength)

    # --- Breakout state masking
    elif kind == "breakout":
        if view.get("retest_time") and view["retest_time"] > t:
            view["retest_time"] = None
            view["outcome"] = None

    # --- Range state masking
    elif kind in ("range",):
        if view.get("end_time") and view["end_time"] > t:
            view["end_time"] = None
            view["breakout_direction"] = None
            view["state"] = "active"

    return view


def _sr_state_before_retired(view: dict[str, Any], t: int) -> ZoneState:
    if view.get("flipped_at") and view["flipped_at"] <= t:
        return "flipped"
    if view.get("broken_at") and view["broken_at"] <= t:
        return "broken"
    return "active"


def reconstruct_state(items: list[dict[str, Any]], t: int) -> dict[str, Any]:
    """Reconstruct state as of epoch ms T from item log (spec §0)."""
    views = [v for it in items if (v := as_of(it, t)) is not None]
    zones = {v["id"]: v for v in views if v.get("kind") in ("sr_support", "sr_resistance")}
    breakouts = [v for v in views if v.get("kind") == "breakout"]
    patterns = [v for v in views if v.get("kind") == "pattern"]
    ranges = [v for v in views if v.get("kind") == "range"]
    return {"zones": zones, "breakouts": breakouts, "patterns": patterns, "ranges": ranges}

