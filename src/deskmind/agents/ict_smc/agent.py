"""Multi-timeframe ICT/SMC liquidity agent for one symbol.

The single code path for live and batch runs.
`on_bar_close(close_time, bars_by_tf)` is called once per bar-close boundary with the
candles that closed at that time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from deskmind.agents.ict_smc.config import AGENT_NAME, DEFAULT_CONFIG, IctSmcConfig
from deskmind.agents.ict_smc.engine import IctSmcEngine
from deskmind.agents.ict_smc.models import SMTDivergence
from deskmind.agents.trend.config import DEFAULT_CONFIG as DEFAULT_TREND_CONFIG
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.models import Bar
from deskmind.data.datasets import TIMEFRAME_MS

REPORT_TIMEFRAME = "all"


def iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else None


def _iso_times(obj: Any) -> Any:
    """Add *_iso siblings for epoch-ms fields to make reports readable."""
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
    report: dict[str, Any]
    changes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (timeframe, item)


class SymbolIctSmcAgent:
    def __init__(
        self,
        symbol: str,
        timeframes: Iterable[str],
        cfg: IctSmcConfig = DEFAULT_CONFIG,
        trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
    ) -> None:
        self.symbol = symbol
        self.timeframes = list(timeframes)
        self.cfg = cfg
        self.trend_cfg = trend_cfg
        self.engines = {tf: IctSmcEngine(symbol, tf, cfg, trend_cfg) for tf in self.timeframes}

    def on_bar_close(self, close_time: int, bars_by_tf: dict[str, list[Bar]]) -> AgentOutput:
        changes: list[tuple[str, dict[str, Any]]] = []
        for tf in self.timeframes:
            for bar in sorted(bars_by_tf.get(tf, []), key=lambda b: b.open_time):
                if bar.close_time > close_time:
                    continue  # never feed a candle that has not closed at this boundary
                changes.extend((tf, item) for item in self.engines[tf].on_candle(bar))
        return AgentOutput(self.report(close_time), changes)

    def report(self, as_of: int) -> dict[str, Any]:
        return {
            "agent": AGENT_NAME,
            "agent_version": self.cfg.version,
            "symbol": self.symbol,
            "as_of": iso(as_of),
            "as_of_ms": as_of,
            "timeframes": {tf: _iso_times(self.engines[tf].state()) for tf in self.timeframes},
        }


def check_smt_divergence(
    agent_a: SymbolIctSmcAgent,
    agent_b: SymbolIctSmcAgent,
    timeframe: str,
    close_time: int,
) -> SMTDivergence | None:
    """Evaluate SMT divergence between two agents for a specific timeframe at close_time.

    Only evaluated when both have confirmed swing data aligned by candle close time.
    """
    eng_a = agent_a.engines.get(timeframe)
    eng_b = agent_b.engines.get(timeframe)
    if eng_a is None or eng_b is None:
        return None

    # Get recent confirmed swings from both engines
    swings_a = list(eng_a.confirmed_swings)
    swings_b = list(eng_b.confirmed_swings)
    if len(swings_a) < 2 or len(swings_b) < 2:
        return None

    # Check Highs (Bearish SMT)
    highs_a = [s for s in swings_a if s["kind"] == "swing_high"]
    highs_b = [s for s in swings_b if s["kind"] == "swing_high"]
    if len(highs_a) >= 2 and len(highs_b) >= 2:
        last_a = highs_a[-1]
        prev_a = highs_a[-2]
        last_b = highs_b[-1]
        prev_b = highs_b[-2]

        # Aligned: confirmed at the same close time
        if last_a["confirmed_at"] == last_b["confirmed_at"] == close_time:
            a_higher = last_a["price"] > prev_a["price"]
            b_higher = last_b["price"] > prev_b["price"]
            if a_higher and not b_higher:
                smt = SMTDivergence(
                    pair=(agent_a.symbol, agent_b.symbol),
                    divergence_type="bearish",
                    time=last_a["pivot_time"],
                    confirmed_at=close_time,
                    symbol_a=agent_a.symbol,
                    symbol_b=agent_b.symbol,
                    note=f"{agent_a.symbol} made HH while {agent_b.symbol} made LH (Bearish SMT)",
                )
                eng_a.last_smt = smt
                eng_b.last_smt = smt
                return smt
            elif not a_higher and b_higher:
                smt = SMTDivergence(
                    pair=(agent_a.symbol, agent_b.symbol),
                    divergence_type="bearish",
                    time=last_b["pivot_time"],
                    confirmed_at=close_time,
                    symbol_a=agent_a.symbol,
                    symbol_b=agent_b.symbol,
                    note=f"{agent_b.symbol} made HH while {agent_a.symbol} made LH (Bearish SMT)",
                )
                eng_a.last_smt = smt
                eng_b.last_smt = smt
                return smt

    # Check Lows (Bullish SMT)
    lows_a = [s for s in swings_a if s["kind"] == "swing_low"]
    lows_b = [s for s in swings_b if s["kind"] == "swing_low"]
    if len(lows_a) >= 2 and len(lows_b) >= 2:
        last_a = lows_a[-1]
        prev_a = lows_a[-2]
        last_b = lows_b[-1]
        prev_b = lows_b[-2]

        if last_a["confirmed_at"] == last_b["confirmed_at"] == close_time:
            a_lower = last_a["price"] < prev_a["price"]
            b_lower = last_b["price"] < prev_b["price"]
            if a_lower and not b_lower:
                smt = SMTDivergence(
                    pair=(agent_a.symbol, agent_b.symbol),
                    divergence_type="bullish",
                    time=last_a["pivot_time"],
                    confirmed_at=close_time,
                    symbol_a=agent_a.symbol,
                    symbol_b=agent_b.symbol,
                    note=f"{agent_a.symbol} made LL while {agent_b.symbol} made HL (Bullish SMT)",
                )
                eng_a.last_smt = smt
                eng_b.last_smt = smt
                return smt
            elif not a_lower and b_lower:
                smt = SMTDivergence(
                    pair=(agent_a.symbol, agent_b.symbol),
                    divergence_type="bullish",
                    time=last_b["pivot_time"],
                    confirmed_at=close_time,
                    symbol_a=agent_a.symbol,
                    symbol_b=agent_b.symbol,
                    note=f"{agent_b.symbol} made LL while {agent_a.symbol} made HL (Bullish SMT)",
                )
                eng_a.last_smt = smt
                eng_b.last_smt = smt
                return smt

    return None


@dataclass
class BatchResult:
    reports: list[dict[str, Any]]
    items: dict[tuple[str, str], dict[str, Any]]  # (timeframe, id) -> latest item version
    boundaries: int = 0


def run_batch(
    agent: SymbolIctSmcAgent,
    bars_by_tf: dict[str, list[Bar]],
    *,
    report_from: int | None = None,
    store_from: int | None = None,
) -> BatchResult:
    """Replay history through `agent.on_bar_close`, one bar-close boundary at a time."""
    base = min(TIMEFRAME_MS[tf] for tf in agent.timeframes)
    by_close: dict[int, dict[str, list[Bar]]] = {}
    for tf, bars in bars_by_tf.items():
        for b in bars:
            by_close.setdefault(b.close_time, {}).setdefault(tf, []).append(b)

    reports: list[dict[str, Any]] = []
    items: dict[tuple[str, str], dict[str, Any]] = {}
    boundaries = 0

    for t in sorted(by_close):
        if t % base:
            continue
        out = agent.on_bar_close(t, by_close[t])
        boundaries += 1
        if report_from is None or t >= report_from:
            reports.append(out.report)
        for tf, item in out.changes:
            if store_from is None or item["confirmed_at"] >= store_from:
                items[(tf, item["id"])] = item

    return BatchResult(reports, items, boundaries)
