"""Point-in-time storage for the ICT Time agent: items + state transitions.

Mirrors the ICT/SMC pattern: TransitionRecorder subclass + reconstruct_state.
Items: sessions and key levels. State synthetic rows: killzone, AMD, weekend flag.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from deskmind.agents.transitions import STATE_BASELINE, TransitionRecorder, replay_items

if TYPE_CHECKING:
    from deskmind.agents.ict_time.config import IctTimeConfig
    from deskmind.agents.ict_time.engine import IctTimeEngine

STATE_KILLZONE = "state:killzone"
STATE_AMD = "state:amd"
STATE_WEEKEND = "state:weekend"


class IctTimeTransitionRecorder(TransitionRecorder):
    """ICT Time hooks for the shared transition recorder."""

    def objects(self, engine: IctTimeEngine) -> list[Any]:  # type: ignore[override]
        return [*engine.sessions.values(), *engine.key_levels.values()]

    def record_states(self, at: int, tf: str, engine: IctTimeEngine) -> None:  # type: ignore[override]
        # tf is irrelevant for ICT Time (no timeframe concept), but signature matches
        state = engine.state()
        self.record_value(at, tf, STATE_KILLZONE, state["killzone"])
        self.record_value(at, tf, STATE_AMD, state["amd"])
        self.record_value(at, tf, STATE_WEEKEND, state["weekend_flag"])


def reconstruct_state(
    rows: list[dict[str, Any]],
    at: int,
    cfg: IctTimeConfig,
) -> dict[str, Any]:
    """`IctTimeEngine.state()` as of boundary `at`, from transition rows."""
    from deskmind.agents.ict_time.models import as_of as item_as_of

    items, rank, states = replay_items(rows, at)
    ordered = sorted(items.values(), key=lambda it: rank[it["id"]])

    sessions: dict[str, Any] = {}
    key_levels: list[dict[str, Any]] = []

    for it in ordered:
        kind = it.get("kind", "")
        view = item_as_of(it, at)
        if view is None:
            continue
        if kind.startswith("session_"):
            name = kind[len("session_"):]
            sessions[name] = {
                "id": it["id"],
                "name": name,
                "state": view.get("state"),
                "high": view.get("high"),
                "low": view.get("low"),
                "open": view.get("open"),
                "swept_high_by": view.get("swept_high_by"),
                "swept_low_by": view.get("swept_low_by"),
            }
        elif kind in (
            "pdh", "pdl", "pwh", "pwl", "pmh", "pml",
            "daily_open", "weekly_open", "monthly_open",
            "ny_midnight_open", "asian_range_high", "asian_range_low",
        ):
            if view.get("state") != "taken":
                key_levels.append({
                    "id": it["id"],
                    "level_type": kind,
                    "price": view.get("price"),
                    "state": view.get("state"),
                    "confirmed_at": view.get("confirmed_at"),
                    "swept_at": view.get("swept_at"),
                    "swept_by": view.get("swept_by"),
                    "distance_atr": None,  # computed at render time
                })

    return {
        "sessions": sessions,
        "current_session": next(
            (v["name"] for v in sessions.values() if v.get("state") == "active"), None
        ),
        "killzone": states.get(STATE_KILLZONE) or {"current": None, "minutes_to_next": None},
        "silver_bullet": {"active": None, "fvg_in_windows": []},
        "key_levels": key_levels,
        "weekend_flag": states.get(STATE_WEEKEND, False),
        "amd": states.get(STATE_AMD) or {
            "phase": "unknown", "confidence": "low", "heuristic": True
        },
    }


def reconstruct_report(
    symbol: str,
    rows: list[dict[str, Any]],
    at: int,
    cfg: IctTimeConfig,
) -> dict[str, Any]:
    """Full agent report as of `at`, rebuilt from transition rows."""
    from deskmind.agents.ict_time.agent import AGENT_NAME, _iso_times, iso

    return {
        "agent": AGENT_NAME,
        "agent_version": cfg.version,
        "symbol": symbol,
        "as_of": iso(at),
        "as_of_ms": at,
        "timeframe": "all",
        **_iso_times(reconstruct_state(rows, at, cfg)),
        "reconstructed": True,
    }


__all__ = [
    "STATE_BASELINE",
    "IctTimeTransitionRecorder",
    "reconstruct_report",
    "reconstruct_state",
    "replay_items",
]
