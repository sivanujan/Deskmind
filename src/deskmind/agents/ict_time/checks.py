"""Truncation and convergence checks for the ICT Time agent."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from deskmind.agents.ict_time.config import DEFAULT_CONFIG, IctTimeConfig
from deskmind.agents.ict_time.engine import IctTimeEngine
from deskmind.agents.ict_time.models import as_of
from deskmind.agents.trend.models import Bar


@dataclass
class TruncationResult:
    cuts: int
    passed: int
    items_compared: int
    failures: list[str]


def _run(
    bars: list[Bar],
    cfg: IctTimeConfig,
    capture: set[int] | None = None,
) -> tuple[IctTimeEngine, dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    eng = IctTimeEngine("X", cfg)
    items: dict[str, dict[str, Any]] = {}
    states: dict[int, dict[str, Any]] = {}
    for b in bars:
        for it in eng.on_candle(b):
            items[it["id"]] = it
        if capture is not None and b.close_time in capture:
            states[b.close_time] = eng.state()
    return eng, items, states


def _view(items: dict[str, dict[str, Any]], t: int) -> dict[str, dict[str, Any]]:
    out = {}
    for k, it in items.items():
        v = as_of(it, t)
        if v is not None:
            out[k] = v
    return out


def truncation_check(
    bars: list[Bar],
    n_cuts: int = 10,
    seed: int = 42,
    cfg: IctTimeConfig = DEFAULT_CONFIG,
) -> TruncationResult:
    """No-look-ahead: items produced by a truncated run == as-of view at that time."""
    rng = random.Random(seed)
    warmup = 50  # allow some warmup before cutting
    lo = min(len(bars) - 1, warmup)
    idx = sorted(rng.sample(range(lo, len(bars)), min(n_cuts, max(1, len(bars) - lo))))
    cuts = {bars[i].close_time for i in idx}
    _, full_items, full_states = _run(bars, cfg, capture=cuts)
    failures: list[str] = []
    failed: set[int] = set()
    compared = 0
    for i in idx:
        t = bars[i].close_time
        _, trunc_items, trunc_states = _run(bars[: i + 1], cfg, capture={t})
        if full_states.get(t) != trunc_states.get(t):
            failed.add(t)
            failures.append(f"state differs at {t}")
        a, b = _view(full_items, t), _view(trunc_items, t)
        compared += len(b)
        if a != b:
            diff = sorted(set(a) ^ set(b))[:3] or [k for k in a if a[k] != b.get(k)][:3]
            failed.add(t)
            failures.append(f"items differ at {t}: {diff}")
    return TruncationResult(len(idx), len(idx) - len(failed), compared, failures)


def convergence_check(
    bars: list[Bar],
    offset: int,
    cfg: IctTimeConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Items produced from offset onward match the full run."""
    _, full, _ = _run(bars, cfg)
    _, late, _ = _run(bars[offset:], cfg)
    if not bars:
        return {"converged_after_bars": 0, "last_disagreement": None}
    start_t = bars[offset].open_time
    full_after = {k: v for k, v in full.items() if v["confirmed_at"] >= start_t}
    disagree = sorted(
        {v["confirmed_at"] for k, v in full_after.items() if late.get(k) != v}
        | {v["confirmed_at"] for k, v in late.items() if full_after.get(k) != v}
    )
    if not disagree:
        return {"converged_after_bars": 0, "last_disagreement": None}
    last = disagree[-1]
    after = next((i for i, b in enumerate(bars[offset:]) if b.close_time > last), None)
    return {"converged_after_bars": after, "last_disagreement": last}
