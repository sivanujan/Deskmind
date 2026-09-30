"""Verification helpers for ICT/SMC agent: truncation test (no look-ahead) and convergence."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from deskmind.agents.ict_smc.config import DEFAULT_CONFIG, IctSmcConfig
from deskmind.agents.ict_smc.engine import IctSmcEngine
from deskmind.agents.ict_smc.models import as_of
from deskmind.agents.trend.config import DEFAULT_CONFIG as DEFAULT_TREND_CONFIG
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.models import Bar


@dataclass
class TruncationResult:
    cuts: int
    passed: int
    items_compared: int
    failures: list[str]


def _run(
    bars: list[Bar],
    cfg: IctSmcConfig,
    trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
    capture: set[int] | None = None,
):
    eng = IctSmcEngine("X", "tf", cfg, trend_cfg)
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
    seed: int = 7,
    cfg: IctSmcConfig = DEFAULT_CONFIG,
    trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
) -> TruncationResult:
    rng = random.Random(seed)
    lo = min(len(bars) - 1, trend_cfg.atr_period + trend_cfg.left_bars + trend_cfg.right_bars + 5)
    idx = sorted(rng.sample(range(lo, len(bars)), min(n_cuts, max(1, len(bars) - lo))))
    cuts = {bars[i].close_time for i in idx}
    _, full_items, full_states = _run(bars, cfg, trend_cfg, capture=cuts)
    failures: list[str] = []
    failed: set[int] = set()
    compared = 0
    for i in idx:
        t = bars[i].close_time
        _, trunc_items, trunc_states = _run(bars[: i + 1], cfg, trend_cfg, capture={t})
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
    cfg: IctSmcConfig = DEFAULT_CONFIG,
    trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
) -> dict[str, Any]:
    _, full, _ = _run(bars, cfg, trend_cfg)
    _, late, _ = _run(bars[offset:], cfg, trend_cfg)
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
