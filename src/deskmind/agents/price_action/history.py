"""Point-in-time history for the Price Action agent.

Pattern follows the ICT/SMC agent's history.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deskmind.agents.transitions import STATE_BASELINE, TransitionRecorder, replay_items

if TYPE_CHECKING:
    from deskmind.agents.price_action.config import PriceActionConfig
    from deskmind.agents.price_action.engine import PriceActionEngine

STATE_SR_SUMMARY = "state:sr_summary"


class PriceActionTransitionRecorder(TransitionRecorder):
    def objects(self, engine: PriceActionEngine) -> list[Any]:  # type: ignore[override]
        zones = list(engine.zones.values())
        bos = engine.breakouts
        rng_list = [engine.active_range] if engine.active_range else []
        pats = list(engine.patterns)
        return [*zones, *bos, *rng_list, *pats]

    def record_states(self, at: int, tf: str, engine: PriceActionEngine) -> None:  # type: ignore[override]
        s = engine.state()
        self.record_value(at, tf, STATE_SR_SUMMARY, {
            "nearest_support": s["nearest_support"],
            "nearest_resistance": s["nearest_resistance"],
        })


def reconstruct_state(
    rows: list[dict[str, Any]],
    at: int,
    cfg: PriceActionConfig,
    tf: str,
) -> dict[str, Any]:
    from deskmind.agents.price_action.models import as_of as item_as_of

    items, rank, states = replay_items(rows, at)
    ordered = sorted(items.values(), key=lambda it: rank[it["id"]])

    zones: list[dict[str, Any]] = []
    breakouts: list[dict[str, Any]] = []
    patterns: list[dict[str, Any]] = []
    ranges: list[dict[str, Any]] = []

    for it in ordered:
        kind = it.get("kind", "")
        view = item_as_of(it, at)
        if view is None:
            continue
        if kind in ("sr_support", "sr_resistance"):
            if view.get("state") != "retired":
                zones.append(view)
        elif kind == "breakout":
            if view.get("outcome") is None:
                breakouts.append(view)
        elif kind == "range":
            if view.get("state") == "active":
                ranges.append(view)
        elif kind.startswith("pattern_"):
            patterns.append(view)

    sr = states.get(STATE_SR_SUMMARY, {})
    return {
        "active_zones": zones,
        "nearest_support": sr.get("nearest_support"),
        "nearest_resistance": sr.get("nearest_resistance"),
        "active_range": ranges[-1] if ranges else None,
        "recent_patterns": patterns[-10:],
        "recent_breakouts": breakouts,
    }


def reconstruct_report(
    symbol: str,
    tf: str,
    rows: list[dict[str, Any]],
    at: int,
    cfg: PriceActionConfig,
) -> dict[str, Any]:
    from deskmind.agents.price_action.agent import AGENT_NAME, iso

    state = reconstruct_state(rows, at, cfg, tf)
    return {
        "agent": AGENT_NAME,
        "agent_version": cfg.version,
        "symbol": symbol,
        "timeframe": tf,
        "as_of": iso(at),
        "as_of_ms": at,
        **state,
        "reconstructed": True,
    }


__all__ = [
    "STATE_BASELINE",
    "PriceActionTransitionRecorder",
    "reconstruct_report",
    "reconstruct_state",
    "replay_items",
]
