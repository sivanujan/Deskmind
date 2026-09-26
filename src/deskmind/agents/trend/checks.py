"""Verification helpers: no-look-ahead truncation test and start-point convergence.

Truncation: run the engine on all candles, recording its state right after the candle that
closes at each cut point T; run it again on candles truncated at T. The state as of T and every
item as known at T (models.as_of) must be identical. An item whose confirmed_at claimed an
earlier time than when it was actually produced (or a retro-active change) would differ.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from deskmind.agents.trend.config import DEFAULT_CONFIG, TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.models import Bar, as_of


@dataclass
class TruncationResult:
    cuts: int
    passed: int
    items_compared: int
    failures: list[str]


def _run(bars: list[Bar], cfg: TrendConfig, capture: set[int] | None = None):
    eng = TrendEngine("X", "tf", cfg)
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
    bars: list[Bar], n_cuts: int = 10, seed: int = 7, cfg: TrendConfig = DEFAULT_CONFIG
) -> TruncationResult:
    rng = random.Random(seed)
    lo = min(len(bars) - 1, cfg.atr_period + cfg.left_bars + cfg.right_bars + 5)
    idx = sorted(rng.sample(range(lo, len(bars)), min(n_cuts, len(bars) - lo)))
    cuts = {bars[i].close_time for i in idx}
    _, full_items, full_states = _run(bars, cfg, capture=cuts)
    failures: list[str] = []
    failed: set[int] = set()
    compared = 0
    for i in idx:
        t = bars[i].close_time
        _, trunc_items, trunc_states = _run(bars[: i + 1], cfg, capture={t})
        if full_states[t] != trunc_states[t]:
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
    bars: list[Bar], offset: int, cfg: TrendConfig = DEFAULT_CONFIG
) -> dict[str, Any]:
    """Start one run `offset` candles later; report when both runs start agreeing for good.

    Returns the number of candles after the later start until the (swing/break/line) items
    and the state agree for the rest of the data, or None if they never fully agree.
    """
    _, full, _ = _run(bars, cfg)
    _, late, _ = _run(bars[offset:], cfg)
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
