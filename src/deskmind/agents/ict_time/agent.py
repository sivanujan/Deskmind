"""Single-symbol ICT Time agent: wraps the engine for live and batch runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from deskmind.agents.ict_time.config import AGENT_NAME, DEFAULT_CONFIG, IctTimeConfig
from deskmind.agents.ict_time.engine import IctTimeEngine
from deskmind.agents.ict_time.history import IctTimeTransitionRecorder as TransitionRecorder
from deskmind.agents.trend.models import Bar
from deskmind.data.datasets import TIMEFRAME_MS


@lru_cache(maxsize=65536)
def iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else None


def _iso_times(obj: Any) -> Any:
    """Add *_iso siblings for epoch-ms fields."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out[k] = _iso_times(v)
            if isinstance(v, int) and (k.endswith("time") or k.endswith("_at") or k == "as_of"):
                out[f"{k}_iso"] = iso(v)
        return out
    elif isinstance(obj, list):
        return [_iso_times(x) for x in obj]
    return obj


@dataclass
class AgentOutput:
    report: dict[str, Any] | None
    changes: list[dict[str, Any]] = field(default_factory=list)


class IctTimeAgent:
    """Per-symbol ICT Time agent (all timeframes share one engine)."""

    def __init__(self, symbol: str, cfg: IctTimeConfig = DEFAULT_CONFIG) -> None:
        self.symbol = symbol
        self.cfg = cfg
        # ICT Time is symbol-level (not per-timeframe): one engine per symbol
        self.engine = IctTimeEngine(symbol, cfg)
        # Expose engines dict for compatibility with generic TransitionRecorder.baseline()
        self.engines = {"all": self.engine}

    def on_bar_close(
        self,
        close_time: int,
        bars_by_tf: dict[str, list[Bar]],
        *,
        with_report: bool = True,
    ) -> AgentOutput:
        """Process bar_close event. bars_by_tf may contain multiple timeframes."""
        changes: list[dict[str, Any]] = []
        # Feed the lowest-granularity bars to the engine (all timeframes feed state)
        all_bars: list[Bar] = []
        for bars in bars_by_tf.values():
            all_bars.extend(bars)
        # Process each bar in time order
        for bar in sorted(all_bars, key=lambda b: b.open_time):
            if bar.close_time > close_time:
                continue
            changes.extend(self.engine.on_candle(bar))
        report = self.report(close_time) if with_report else None
        return AgentOutput(report=report, changes=changes)

    def report(self, as_of: int) -> dict[str, Any]:
        return {
            "agent": AGENT_NAME,
            "agent_version": self.cfg.version,
            "symbol": self.symbol,
            "as_of": iso(as_of),
            "as_of_ms": as_of,
            **_iso_times(self.engine.state()),
        }


@dataclass
class BatchResult:
    reports: list[dict[str, Any]]
    items: list[dict[str, Any]]
    boundaries: int = 0
    recorder: TransitionRecorder | None = None


def run_batch(
    agent: IctTimeAgent,
    bars_by_tf: dict[str, list[Bar]],
    *,
    report_from: int | None = None,
    store_from: int | None = None,
    reports: bool = True,
    recorder: TransitionRecorder | None = None,
) -> BatchResult:
    """Replay history, one bar-close boundary at a time."""
    base = min(TIMEFRAME_MS[tf] for tf in bars_by_tf if tf in TIMEFRAME_MS)
    by_close: dict[int, dict[str, list[Bar]]] = {}
    for tf, bars in bars_by_tf.items():
        for b in bars:
            by_close.setdefault(b.close_time, {}).setdefault(tf, []).append(b)

    report_list: list[dict[str, Any]] = []
    all_items: dict[str, dict[str, Any]] = {}
    boundaries = 0

    for t in sorted(by_close):
        if t % base:
            continue
        closed_bars = by_close.get(t, {})
        if not closed_bars or not any(closed_bars.values()):
            continue
        want = reports and (report_from is None or t >= report_from)
        out = agent.on_bar_close(t, closed_bars, with_report=want)
        boundaries += 1
        if out.report is not None:
            report_list.append(out.report)
        if recorder is not None and (store_from is None or t >= store_from):
            if recorder.started:
                recorder.boundary(t, agent, [(None, ch) for ch in out.changes])
            else:
                recorder.baseline(t, agent)
        if store_from is None or t >= store_from:
            for ch in out.changes:
                if store_from is None or ch.get("confirmed_at", 0) >= store_from:
                    all_items[ch["id"]] = ch

    return BatchResult(report_list, list(all_items.values()), boundaries, recorder)
