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

from typing import TYPE_CHECKING, Any

from deskmind.agents.transitions import STATE_BASELINE, TransitionRecorder, replay_items

if TYPE_CHECKING:
    from deskmind.agents.ict_smc.config import IctSmcConfig
    from deskmind.agents.ict_smc.engine import IctSmcEngine

STATE_PD = "state:pd_range"
STATE_SWEEP = "state:last_sweep"
STATE_SMT = "state:smt"
PD_BASE_KEYS = ("low", "high", "from_bos_at", "equilibrium", "ote_low", "ote_high", "confirmed_at")

# Fields each summary() exposes, taken from the item payload (models.py summary methods).
SUMMARY_KEYS = {
    "pool": ("id", "level", "pool_type", "strength", "state", "created_time", "confirmed_at",
             "swept_at", "taken_at"),
    "fvg": ("id", "direction", "top", "bottom", "ce", "atr", "size_atr", "displacement_backed",
            "state", "created_time", "confirmed_at"),
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


class IctTransitionRecorder(TransitionRecorder):
    """ICT/SMC hooks for the shared recorder (deskmind.agents.transitions)."""

    def objects(self, engine: IctSmcEngine) -> list[Any]:
        return [*engine.displacements, *engine.pools, *engine.fvgs, *engine.order_blocks,
                *engine.breakers, *engine.voids]  # fmt: skip

    def record_states(self, at: int, tf: str, engine: IctSmcEngine) -> None:
        # the PD range's moving fields are derived from the close: only a new range counts
        self.record_value(
            at, tf, STATE_PD, _pd_payload(engine),
            same=lambda a, b: all(a[k] == b[k] for k in PD_BASE_KEYS),
        )  # fmt: skip
        self.record_value(at, tf, STATE_SWEEP, engine.last_sweep)
        self.record_value(at, tf, STATE_SMT, _smt_payload(engine))


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


__all__ = ["STATE_BASELINE", "IctTransitionRecorder", "reconstruct_report", "reconstruct_state",
           "replay_items"]  # fmt: skip
