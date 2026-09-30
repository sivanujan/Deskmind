"""Point-in-time storage for the ICT/SMC agent: items + state transitions instead of snapshots.

Source of truth (per symbol, timeframe, agent_version):
- one transition row per item change, ordered by (at, ord): `at` = bar-close boundary at which the
  engine emitted it, `ord` = emission order within that boundary. The first row of an item holds
  its full payload, later rows only the fields that changed.
- synthetic "state" rows for engine state that is not an item: the PD range (stored only when the
  range itself changes; its position/zone/in_ote are re-derived from the candle close), the last
  sweep and the last SMT divergence.
- a baseline at the first stored boundary: every object the engine still holds (in list order),
  so state at any later T is reconstructable even for items created during warm-up.

`reconstruct_state(rows, T, cfg, last_close)` rebuilds exactly `IctSmcEngine.state()` as of T
(tests compare the two at random T). Full multi-timeframe reports are therefore only stored for
live runs, with a retention window.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskmind.agents.ict_smc.agent import SymbolIctSmcAgent
    from deskmind.agents.ict_smc.config import IctSmcConfig
    from deskmind.agents.ict_smc.engine import IctSmcEngine

STATE_PD = "state:pd_range"
STATE_SWEEP = "state:last_sweep"
STATE_SMT = "state:smt"
STATE_BASELINE = "state:baseline"  # marker: replay restarts here (a new baseline follows)
PD_BASE_KEYS = ("low", "high", "from_bos_at", "equilibrium", "ote_low", "ote_high", "confirmed_at")

# Fields each summary() exposes, taken from the item payload (models.py summary methods).
SUMMARY_KEYS = {
    "pool": ("id", "level", "pool_type", "strength", "state", "created_time", "confirmed_at",
             "swept_at", "taken_at"),
    "fvg": ("id", "direction", "top", "bottom", "ce", "atr", "state", "created_time",
            "confirmed_at"),
    "ob": ("id", "direction", "top", "bottom", "mt", "atr", "state", "created_time",
           "confirmed_at"),
    "breaker": ("id", "direction", "is_mitigation", "top", "bottom", "atr", "state",
                "created_time", "confirmed_at"),
}  # fmt: skip


def family(kind: str) -> str | None:
    if kind in ("bsl", "ssl"):
        return "pool"
    if kind.startswith("fvg_"):
        return "fvg"
    if kind.startswith("ob_"):
        return "ob"
    if kind.startswith(("breaker_", "mitigation_block_")):
        return "breaker"
    if kind == "displacement":
        return "displacement"
    if kind == "liquidity_void":
        return "void"
    return None


def _pd_payload(eng: IctSmcEngine) -> dict[str, Any] | None:
    if eng.pd_range is None:
        return None
    d = eng.pd_range.as_dict()
    # creation-time dynamic values are kept for the degenerate high <= low case
    return {k: d[k] for k in (*PD_BASE_KEYS, "zone", "position_pct", "in_ote")}


def _smt_payload(eng: IctSmcEngine) -> dict[str, Any] | None:
    return eng.last_smt.summary() if eng.last_smt else None


@dataclass
class TransitionRecorder:
    """Turns engine output into transition rows. One per symbol; keeps the last payload per item."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    _last: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    _ord: dict[int, int] = field(default_factory=dict)
    started: bool = False

    def _emit(self, at: int, tf: str, item_id: str, kind: str, payload: dict[str, Any]) -> None:
        o = self._ord.get(at, 0)
        self._ord[at] = o + 1
        self.rows.append(
            {"tf": tf, "item_id": item_id, "kind": kind, "at": at, "ord": o, "payload": payload}
        )

    def record(self, at: int, tf: str, item: dict[str, Any]) -> None:
        key = (tf, item["id"])
        prev = self._last.get(key)
        if prev is None:
            self._emit(at, tf, item["id"], item["kind"], copy.deepcopy(item))
        else:
            diff = {k: v for k, v in item.items() if prev.get(k) != v}
            if not diff:
                return
            self._emit(at, tf, item["id"], item["kind"], copy.deepcopy(diff))
        self._last[key] = copy.deepcopy(item)

    def record_state(self, at: int, tf: str, eng: IctSmcEngine) -> None:
        for item_id, value in (
            (STATE_PD, _pd_payload(eng)),
            (STATE_SWEEP, eng.last_sweep),
            (STATE_SMT, _smt_payload(eng)),
        ):
            key = (tf, item_id)
            prev = self._last.get(key, {"value": None})
            # the PD range's moving fields are derived from the close: only a new range counts
            same = (
                prev["value"] is not None
                and value is not None
                and all(prev["value"][k] == value[k] for k in PD_BASE_KEYS)
                if item_id == STATE_PD
                else prev["value"] == value
            )
            if same or (prev["value"] is None and value is None):
                continue
            self._emit(at, tf, item_id, item_id, {"value": copy.deepcopy(value)})
            self._last[key] = {"value": copy.deepcopy(value)}

    def baseline(self, at: int, agent: SymbolIctSmcAgent) -> None:
        """Full payload of every object the engines hold, in list order (first stored boundary)."""
        self.started = True
        self._last.clear()
        for tf, eng in agent.engines.items():
            self._emit(at, tf, STATE_BASELINE, STATE_BASELINE, {"value": None})
            for obj in [*eng.displacements, *eng.pools, *eng.fvgs, *eng.order_blocks,
                        *eng.breakers, *eng.voids]:  # fmt: skip
                self.record(at, tf, obj.item())
            self.record_state(at, tf, eng)

    def boundary(self, at: int, agent: SymbolIctSmcAgent, changes: list[tuple[str, dict]]) -> None:
        for tf, item in changes:
            self.record(at, tf, item)
        for tf, eng in agent.engines.items():
            self.record_state(at, tf, eng)


def replay_items(
    rows: list[dict[str, Any]], at_most: int
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, Any]]:
    """Apply rows (one symbol+tf, sorted by (at, ord)) up to `at_most`.

    Returns (items by id, creation rank by id, state values by state id)."""
    items: dict[str, dict[str, Any]] = {}
    rank: dict[str, int] = {}
    states: dict[str, Any] = {}
    for n, r in enumerate(rows):
        if r["at"] > at_most:
            break
        if r["item_id"] == STATE_BASELINE:  # everything before it is superseded
            items, rank, states = {}, {}, {}
            continue
        if r["item_id"].startswith("state:"):
            states[r["item_id"]] = r["payload"]["value"]
            continue
        cur = items.get(r["item_id"])
        if cur is None:
            items[r["item_id"]] = copy.deepcopy(r["payload"])
            rank[r["item_id"]] = n
        else:
            cur.update(copy.deepcopy(r["payload"]))
    return items, rank, states


def _pd_at(pd: dict[str, Any] | None, last_close: float | None) -> dict[str, Any] | None:
    """PD range as the engine would show it after a bar closing at `last_close`."""
    if pd is None:
        return None
    out = dict(pd)
    low, high = pd["low"], pd["high"]
    if last_close is not None and high > low:
        eq = (low + high) / 2.0
        out["position_pct"] = round((last_close - low) / (high - low) * 100, 2)
        out["zone"] = (
            "discount" if last_close < eq else "premium" if last_close > eq else "equilibrium"
        )
        out["in_ote"] = pd["ote_low"] <= last_close <= pd["ote_high"]
    return {
        k: out[k]
        for k in ("low", "high", "from_bos_at", "equilibrium", "zone", "position_pct", "in_ote",
                  "ote_low", "ote_high", "confirmed_at")
    }  # fmt: skip


def reconstruct_state(
    rows: list[dict[str, Any]], at: int, cfg: IctSmcConfig, last_close: float | None
) -> dict[str, Any]:
    """`IctSmcEngine.state()` as of boundary `at`, from transition rows of one symbol+timeframe.

    `last_close`: close of the last candle of this timeframe with close_time <= at."""
    items, rank, states = replay_items(rows, at)
    ordered = sorted(items.values(), key=lambda it: rank[it["id"]])

    def summ(fam: str, it: dict[str, Any]) -> dict[str, Any]:
        return {k: it.get(k) for k in SUMMARY_KEYS[fam]}

    pools = [it for it in ordered if family(it["kind"]) == "pool"]
    fvgs = [it for it in ordered if family(it["kind"]) == "fvg" and it["state"] != "filled"]
    obs = [it for it in ordered if family(it["kind"]) == "ob" and it["state"] != "invalidated"]
    breakers = [
        it for it in ordered if family(it["kind"]) == "breaker" and it["state"] != "invalidated"
    ]
    disps = [it for it in ordered if family(it["kind"]) == "displacement"]
    fvgs = fvgs[-cfg.max_active_fvgs :]
    obs = obs[-cfg.max_active_obs :]

    pd = _pd_at(states.get(STATE_PD), last_close)
    in_zone: list[dict[str, Any]] = []
    if pd:
        zone, eq = pd["zone"], pd["equilibrium"]
        if zone == "discount":
            in_zone = [
                summ("ob", o) for o in obs if o["direction"] == "bullish" and o["top"] <= eq
            ] + [summ("fvg", f) for f in fvgs if f["direction"] == "bullish" and f["top"] <= eq]
        elif zone == "premium":
            in_zone = [
                summ("ob", o) for o in obs if o["direction"] == "bearish" and o["bottom"] >= eq
            ] + [summ("fvg", f) for f in fvgs if f["direction"] == "bearish" and f["bottom"] >= eq]
    last_disp = disps[-1] if disps else None
    return {
        "pd_range": pd,
        "liquidity": {
            "bsl": [summ("pool", p) for p in [p for p in pools if p["side"] == "high"][
                -cfg.max_active_pools :]],
            "ssl": [summ("pool", p) for p in [p for p in pools if p["side"] == "low"][
                -cfg.max_active_pools :]],
            "last_sweep": states.get(STATE_SWEEP),
        },
        "fvgs": [summ("fvg", f) for f in fvgs],
        "order_blocks": [summ("ob", o) for o in obs],
        "breakers": [summ("breaker", b) for b in breakers[-cfg.max_active_breakers :]],
        "pd_arrays_in_zone": in_zone,
        "displacement": {
            "direction": last_disp["direction"],
            "time": last_disp["created_time"],
            "confirmed_at": last_disp["confirmed_at"],
            "size": last_disp["size"],
            "atr": last_disp["atr"],
        }
        if last_disp
        else None,
        "smt": states.get(STATE_SMT),
    }  # fmt: skip


def reconstruct_report(
    symbol: str,
    rows_by_tf: dict[str, list[dict[str, Any]]],
    at: int,
    cfg: IctSmcConfig,
    last_close: dict[str, float | None],
) -> dict[str, Any]:
    """The agent report as of `at`, rebuilt from transitions (same shape as agent.report)."""
    from deskmind.agents.ict_smc.agent import AGENT_NAME, _iso_times, iso

    return {
        "agent": AGENT_NAME,
        "agent_version": cfg.version,
        "symbol": symbol,
        "as_of": iso(at),
        "as_of_ms": at,
        "timeframes": {
            tf: _iso_times(reconstruct_state(rows, at, cfg, last_close.get(tf)))
            for tf, rows in rows_by_tf.items()
        },
        "reconstructed": True,
    }
