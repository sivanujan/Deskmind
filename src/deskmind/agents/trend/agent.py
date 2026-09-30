"""Multi-timeframe trend agent for one symbol: the single code path for live and batch runs.

`on_bar_close(close_time, bars_by_tf)` is called once per bar-close boundary with the candles
that closed at (or before, if catching up) that time. Live: from `deskmind:bar_close` events and
the Redis buffers. Batch: `run_batch` walks stored history boundary by boundary through the same
method, so both produce identical reports and items for identical candles.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from deskmind.agents.trend.config import AGENT_NAME, DEFAULT_CONFIG, TrendConfig
from deskmind.agents.trend.engine import TrendEngine
from deskmind.agents.trend.history import TrendTransitionRecorder
from deskmind.agents.trend.models import Bar
from deskmind.data.datasets import TIMEFRAME_MS

REPORT_TIMEFRAME = "all"  # agent_reports.timeframe for multi-timeframe reports


def iso(ms: Any) -> str | None:
    if ms is None:
        return None
    if isinstance(ms, datetime):
        return ms.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ms, (int, float)):
        return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ms)


def _iso_times(obj: Any) -> Any:
    """Add *_iso siblings for epoch-ms fields to make reports readable (values unchanged)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out[k] = _iso_times(v)
            if isinstance(v, int) and (k.endswith("time") or k.endswith("_at") or k == "as_of"):
                out[f"{k}_iso"] = iso(v)
        return out
    return obj


@dataclass
class AgentOutput:
    report: dict[str, Any] | None  # None when the caller did not ask for a report
    changes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (timeframe, item)


class SymbolTrendAgent:
    def __init__(
        self, symbol: str, timeframes: Iterable[str], cfg: TrendConfig = DEFAULT_CONFIG
    ) -> None:
        self.symbol = symbol
        self.timeframes = list(timeframes)
        self.cfg = cfg
        self.engines = {tf: TrendEngine(symbol, tf, cfg) for tf in self.timeframes}

    def on_bar_close(
        self, close_time: int, bars_by_tf: dict[str, list[Bar]], with_report: bool = True
    ) -> AgentOutput:
        changes: list[tuple[str, dict[str, Any]]] = []
        for tf in self.timeframes:
            for bar in sorted(bars_by_tf.get(tf, []), key=lambda b: b.open_time):
                if bar.close_time > close_time:
                    continue  # never feed a candle that has not closed at this boundary
                changes.extend((tf, item) for item in self.engines[tf].on_candle(bar))
        return AgentOutput(self.report(close_time) if with_report else None, changes)

    def report(self, as_of: int) -> dict[str, Any]:
        return {
            "agent": AGENT_NAME,
            "agent_version": self.cfg.version,
            "symbol": self.symbol,
            "as_of": iso(as_of),
            "as_of_ms": as_of,
            "timeframes": {tf: _iso_times(self.engines[tf].state()) for tf in self.timeframes},
        }


@dataclass
class BatchResult:
    reports: list[dict[str, Any]]
    items: dict[tuple[str, str], dict[str, Any]]  # (timeframe, id) -> latest item version
    boundaries: int = 0
    recorder: TrendTransitionRecorder | None = None


def run_batch(
    agent: SymbolTrendAgent,
    bars_by_tf: dict[str, list[Bar]],
    *,
    report_from: int | None = None,
    store_from: int | None = None,
    reports: bool = True,
    recorder: TrendTransitionRecorder | None = None,
) -> BatchResult:
    """Replay history through `agent.on_bar_close`, one bar-close boundary at a time.

    Reports are built only if `reports` and kept for boundaries >= report_from; items whose
    confirmed_at >= store_from. With a recorder, transitions are recorded from the first
    boundary >= store_from (starting with a baseline of everything the engines hold)."""
    base = min(TIMEFRAME_MS[tf] for tf in agent.timeframes)
    by_close: dict[int, dict[str, list[Bar]]] = {}
    for tf, bars in bars_by_tf.items():
        for b in bars:
            by_close.setdefault(b.close_time, {}).setdefault(tf, []).append(b)
    report_list: list[dict[str, Any]] = []
    items: dict[tuple[str, str], dict[str, Any]] = {}
    boundaries = 0
    for t in sorted(by_close):
        if t % base:
            continue  # only real bar-close boundaries (all candles close on the base grid)
        want = reports and (report_from is None or t >= report_from)
        out = agent.on_bar_close(t, by_close[t], with_report=want)
        boundaries += 1
        if out.report is not None:
            report_list.append(out.report)
        if recorder is not None and (store_from is None or t >= store_from):
            if recorder.started:
                recorder.boundary(t, agent, out.changes)
            else:
                recorder.baseline(t, agent)
        for tf, item in out.changes:
            if store_from is None or item["confirmed_at"] >= store_from:
                items[(tf, item["id"])] = item
    return BatchResult(report_list, items, boundaries, recorder)
