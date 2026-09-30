"""Point-in-time storage for the trend agent (shared design: deskmind.agents.transitions).

Stored: swings, breaks, sweeps and trend lines as items + every change; synthetic
`state:trend` = {trend, reason} (it also changes without an item, when a trend times out to
"ranging"). Not stored because they move every candle: the engine's `as_of` (close time of the
last candle) and `atr` (SMA of true range) — both are re-derived from the candles at T with the
engine's own formula (`bar_context`).

`reconstruct_state(rows, T, cfg, as_of, atr)` == `TrendEngine.state()` at T (tests compare them
at random T).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deskmind.agents.transitions import TransitionRecorder, replay_items

if TYPE_CHECKING:
    from deskmind.agents.trend.config import TrendConfig
    from deskmind.agents.trend.engine import TrendEngine
    from deskmind.agents.trend.models import Bar

STATE_TREND = "state:trend"


class TrendTransitionRecorder(TransitionRecorder):
    def objects(self, engine: TrendEngine) -> list[Any]:
        # swings (can still be broken), the last break (reported), active lines (can change)
        last_break = [engine.last_break] if engine.last_break else []
        return [*engine.swings, *last_break, *engine.lines]

    def record_states(self, at: int, tf: str, engine: TrendEngine) -> None:
        self.record_value(at, tf, STATE_TREND, {"trend": engine.trend, "reason": engine.reason})


def bar_context(bars: list[Bar], cfg: TrendConfig) -> tuple[int | None, float | None]:
    """(as_of, atr) the engine reports after processing `bars` (oldest first, >= period+1
    candles give the exact value; the engine sums true ranges oldest -> newest)."""
    if not bars:
        return None, None
    recent = bars[-(cfg.atr_period + 1) :]
    trs = []
    for i, b in enumerate(recent):
        if i == 0:
            continue
        prev = recent[i - 1].close
        trs.append(max(b.high - b.low, abs(b.high - prev), abs(b.low - prev)))
    if len(trs) < cfg.atr_period:
        return bars[-1].close_time, None
    trs = trs[-cfg.atr_period :]
    return bars[-1].close_time, sum(trs) / len(trs)


def _swing_summary(it: dict[str, Any]) -> dict[str, Any]:
    return {k: it.get(k) for k in ("price", "label", "pivot_time", "confirmed_at", "broken_at")}


def _break_summary(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": it["type"],
        "direction": it["direction"],
        "level": it["level"],
        "time": it["pivot_time"],
        "confirmed_at": it["confirmed_at"],
    }


def _last_touch(line: dict[str, Any]) -> int:
    return max(t["pivot_time"] for t in line["touches"])


def _line_summary(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": it["kind"].removeprefix("trendline_"),
        "a_time": it["a_time"],
        "a_price": it["a_price"],
        "slope_per_hour": it["slope_per_ms"] * 3_600_000,
        "touches": len(it["touches"]),
        "last_touch": _last_touch(it),
        "confirmed_at": it["confirmed_at"],
    }


def reconstruct_state(
    rows: list[dict[str, Any]],
    at: int,
    cfg: TrendConfig,
    as_of: int | None,
    atr: float | None,
) -> dict[str, Any]:
    """`TrendEngine.state()` as of boundary `at` from transition rows of one symbol+timeframe."""
    items, rank, states = replay_items(rows, at)
    ordered = sorted(items.values(), key=lambda it: rank[it["id"]])
    highs = [it for it in ordered if it["kind"] == "swing_high"]
    lows = [it for it in ordered if it["kind"] == "swing_low"]
    breaks = [it for it in ordered if it["kind"] in ("bos", "choch")]
    lines = [
        it
        for it in ordered
        if it["kind"].startswith("trendline_")
        and it.get("broken_at") is None
        and it.get("expired_at") is None
    ]
    trend_state = states.get(STATE_TREND) or {"trend": "ranging", "reason": "no_break_yet"}
    trend = trend_state["trend"]
    if trend == "bullish":
        pool = [ln for ln in lines if ln["kind"] == "trendline_support"]
        best = max(pool, key=lambda ln: (len(ln["touches"]), _last_touch(ln)), default=None)
    elif trend == "bearish":
        pool = [ln for ln in lines if ln["kind"] == "trendline_resistance"]
        best = max(pool, key=lambda ln: (len(ln["touches"]), _last_touch(ln)), default=None)
    else:
        best = max(lines, key=_last_touch, default=None)
    return {
        "as_of": as_of,
        "trend": trend,
        "reason": trend_state["reason"],
        "last_swing_high": _swing_summary(highs[-1]) if highs else None,
        "last_swing_low": _swing_summary(lows[-1]) if lows else None,
        "last_event": _break_summary(breaks[-1]) if breaks else None,
        "active_trendline": _line_summary(best) if best else None,
        "atr": atr,
    }


def reconstruct_report(
    symbol: str,
    rows_by_tf: dict[str, list[dict[str, Any]]],
    at: int,
    cfg: TrendConfig,
    context: dict[str, tuple[int | None, float | None]],
) -> dict[str, Any]:
    """The trend report as of `at`, rebuilt from transitions (same shape as agent.report)."""
    from deskmind.agents.trend.agent import AGENT_NAME, _iso_times, iso

    return {
        "agent": AGENT_NAME,
        "agent_version": cfg.version,
        "symbol": symbol,
        "as_of": iso(at),
        "as_of_ms": at,
        "timeframes": {
            tf: _iso_times(reconstruct_state(rows, at, cfg, *context.get(tf, (None, None))))
            for tf, rows in rows_by_tf.items()
        },
    }
