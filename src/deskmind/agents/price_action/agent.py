"""Single-symbol Price Action agent: wraps per-timeframe engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from deskmind.agents.price_action.config import AGENT_NAME, DEFAULT_CONFIG, PriceActionConfig
from deskmind.agents.price_action.engine import PriceActionEngine
from deskmind.agents.price_action.history import PriceActionTransitionRecorder as TransitionRecorder
from deskmind.agents.trend.models import Bar
from deskmind.data.datasets import TIMEFRAME_MS


@lru_cache(maxsize=65536)
def iso(ms: int | None) -> str | None:
    return (
        datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ms else None
    )


@dataclass
class AgentOutput:
    reports: dict[str, dict[str, Any]]                 # tf -> report
    changes: dict[str, list[dict[str, Any]]]            # tf -> [items]


class PriceActionAgent:
    """Per-symbol Price Action agent: one engine per timeframe."""

    def __init__(self, symbol: str, cfg: PriceActionConfig = DEFAULT_CONFIG) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.engines: dict[str, PriceActionEngine] = {}

    def _ensure_engine(self, tf: str) -> PriceActionEngine:
        if tf not in self.engines:
            self.engines[tf] = PriceActionEngine(self.symbol, tf, self.cfg)
        return self.engines[tf]

    def on_bar_close(
        self,
        close_time: int,
        bars_by_tf: dict[str, list[Bar]],
        swings_by_tf: dict[str, list[dict[str, Any]]] | None = None,
        *,
        with_report: bool = True,
    ) -> AgentOutput:
        """Process bar close. `swings_by_tf`: new swing items from Trend agent per tf."""
        swings_by_tf = swings_by_tf or {}
        reports: dict[str, dict[str, Any]] = {}
        changes: dict[str, list[dict[str, Any]]] = {}
        for tf, bars in bars_by_tf.items():
            eng = self._ensure_engine(tf)
            tf_changes: list[dict[str, Any]] = []
            swings = swings_by_tf.get(tf, [])
            for bar in sorted(bars, key=lambda b: b.open_time):
                if bar.close_time > close_time:
                    continue
                tf_changes.extend(eng.on_candle(bar, swings=swings))
            changes[tf] = tf_changes
            if with_report:
                reports[tf] = self.report(tf, close_time)
        return AgentOutput(reports=reports, changes=changes)

    def report(self, tf: str, as_of: int) -> dict[str, Any]:
        eng = self.engines.get(tf)
        state = eng.state() if eng else {}
        return {
            "agent": AGENT_NAME,
            "agent_version": self.cfg.version,
            "symbol": self.symbol,
            "timeframe": tf,
            "as_of": iso(as_of),
            "as_of_ms": as_of,
            **state,
        }


@dataclass
class BatchResult:
    reports: dict[str, list[dict[str, Any]]]           # tf -> [reports]
    items: dict[str, list[dict[str, Any]]]              # tf -> [items]
    boundaries: int = 0
    recorder: TransitionRecorder | None = None


def run_batch(
    agent: PriceActionAgent,
    bars_by_tf: dict[str, list[Bar]],
    swings_by_tf: dict[str, list[dict[str, Any]]] | None = None,
    *,
    report_from: int | None = None,
    store_from: int | None = None,
    reports: bool = True,
    recorder: TransitionRecorder | None = None,
) -> BatchResult:
    """Replay history, one bar-close boundary at a time (lowest tf as the clock)."""
    swings_by_tf = swings_by_tf or {}
    base = min(TIMEFRAME_MS[tf] for tf in bars_by_tf if tf in TIMEFRAME_MS)

    # Index swings by close_time for fast lookup
    swing_index: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for tf, swings in swings_by_tf.items():
        for sw in swings:
            swing_index.setdefault((tf, sw.get("confirmed_at", 0)), []).append(sw)

    by_close: dict[int, dict[str, list[Bar]]] = {}
    for tf, brs in bars_by_tf.items():
        for b in brs:
            by_close.setdefault(b.close_time, {}).setdefault(tf, []).append(b)

    report_list: dict[str, list[dict[str, Any]]] = {}
    all_items: dict[str, dict[str, dict[str, Any]]] = {}  # tf -> {id: item}
    boundaries = 0

    for t in sorted(by_close):
        if t % base:
            continue
        closed_bars = by_close.get(t, {})
        if not closed_bars:
            continue
        want = reports and (report_from is None or t >= report_from)
        # Collect swings confirmed at exactly this bar close
        sw_at: dict[str, list[dict[str, Any]]] = {}
        for tf in closed_bars:
            sws = swing_index.get((tf, t), [])
            if sws:
                sw_at[tf] = sws

        out = agent.on_bar_close(t, closed_bars, sw_at, with_report=want)
        boundaries += 1
        for tf, rpt in out.reports.items():
            report_list.setdefault(tf, []).append(rpt)
        if recorder is not None and (store_from is None or t >= store_from):
            if recorder.started:
                flat_changes = [(tf, ch) for tf, chs in out.changes.items() for ch in chs]
                recorder.boundary(t, agent, flat_changes)
            else:
                recorder.baseline(t, agent)
        if store_from is None or t >= store_from:
            for tf, chs in out.changes.items():
                tf_map = all_items.setdefault(tf, {})
                for ch in chs:
                    if store_from is None or ch.get("confirmed_at", 0) >= store_from:
                        tf_map[ch["id"]] = ch

    items_out: dict[str, list[dict[str, Any]]] = {
        tf: list(v.values()) for tf, v in all_items.items()
    }
    return BatchResult(report_list, items_out, boundaries, recorder)
