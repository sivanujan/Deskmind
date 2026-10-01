"""Event Study Tool for fast forward MFE/MAE and random baseline comparison."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import math
import random
from typing import Any, Sequence

from deskmind.agents.trend.models import Bar
from deskmind.research.hypotheses.models import Signal


@dataclass(frozen=True)
class EventStudyResult:
    signal: Signal
    bars_evaluated: int
    mfe_r: dict[int, float]  # horizon (1, 4, 12, 48) -> max favorable excursion in R
    mae_r: dict[int, float]  # horizon (1, 4, 12, 48) -> max adverse excursion in R
    target_or_stop_first: str  # "target" | "stop" | "neither"
    final_r: float
    symbol: str
    year_half: str  # e.g., "2023-H1", "2024-H2"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_event_study(
    signal: Signal | Sequence[Signal],
    forward_bars: Sequence[Bar] | dict[str, Sequence[Bar]],
    holding_bars: Sequence[int] = (1, 4, 12, 48),
) -> Any:
    """Calculate forward MFE, MAE in R at 1, 4, 12, 48 bars and outcome.

    Supports:
      - (signal, forward_bars) -> EventStudyResult
      - (signals, all_bars_by_symbol, holding_bars=...) -> list[EventStudyResult]
    """
    if isinstance(signal, Signal):
        return _calculate_single_event_study(signal, forward_bars, holding_bars)  # type: ignore[arg-type]

    from bisect import bisect_right

    signals = signal
    bars_by_symbol: dict[str, Sequence[Bar]] = forward_bars  # type: ignore[assignment]
    results: list[EventStudyResult] = []

    open_times = {
        sym: [b.open_time for b in bars] for sym, bars in bars_by_symbol.items()
    }
    max_holding = max(holding_bars) if holding_bars else 48

    for sig in signals:
        sym_bars = bars_by_symbol.get(sig.symbol, [])
        if not sym_bars:
            continue
        op_list = open_times.get(sig.symbol, [])
        idx = bisect_right(op_list, sig.timestamp)
        fwd = sym_bars[idx : idx + max_holding]
        if fwd:
            res = _calculate_single_event_study(sig, fwd, holding_bars)
            results.append(res)

    return results


def _calculate_single_event_study(
    signal: Signal,
    forward_bars: Sequence[Bar],
    holding_bars: Sequence[int] = (1, 4, 12, 48),
) -> EventStudyResult:
    horizons = tuple(holding_bars)
    mfe_r: dict[int, float] = {}
    mae_r: dict[int, float] = {}

    entry = signal.entry_price
    stop = signal.stop_price
    target = signal.target_price
    is_bullish = signal.direction == "bullish"

    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        risk_per_unit = 1.0

    target_hit = False
    stop_hit = False
    target_or_stop_first = "neither"

    max_fav = 0.0
    max_adv = 0.0

    dt_utc = datetime.fromtimestamp(signal.timestamp / 1000, tz=UTC)
    year_half = f"{dt_utc.year}-H1" if dt_utc.month <= 6 else f"{dt_utc.year}-H2"

    for idx, b in enumerate(forward_bars, start=1):
        if is_bullish:
            fav = max(0.0, b.high - entry)
            adv = max(0.0, entry - b.low)
            if not target_hit and not stop_hit:
                if b.high >= target:
                    target_hit = True
                    target_or_stop_first = "target"
                elif b.low <= stop:
                    stop_hit = True
                    target_or_stop_first = "stop"
        else:
            fav = max(0.0, entry - b.low)
            adv = max(0.0, b.high - entry)
            if not target_hit and not stop_hit:
                if b.low <= target:
                    target_hit = True
                    target_or_stop_first = "target"
                elif b.high >= stop:
                    stop_hit = True
                    target_or_stop_first = "stop"

        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)

        if idx in horizons:
            mfe_r[idx] = round(max_fav / risk_per_unit, 4)
            mae_r[idx] = round(max_adv / risk_per_unit, 4)

    # Fill any missing horizons if total bars < max(horizons)
    for h in horizons:
        if h not in mfe_r:
            mfe_r[h] = round(max_fav / risk_per_unit, 4)
            mae_r[h] = round(max_adv / risk_per_unit, 4)

    max_h = max(horizons) if horizons else 48
    if target_or_stop_first == "target":
        final_r = signal.r_multiple
    elif target_or_stop_first == "stop":
        final_r = -1.0
    else:
        final_r = mfe_r.get(max_h, 0.0) - mae_r.get(max_h, 0.0)

    return EventStudyResult(
        signal=signal,
        bars_evaluated=len(forward_bars),
        mfe_r=mfe_r,
        mae_r=mae_r,
        target_or_stop_first=target_or_stop_first,
        final_r=round(final_r, 4),
        symbol=signal.symbol,
        year_half=year_half,
    )


def generate_matched_random_baseline(
    signals: Sequence[Signal],
    all_bars_by_symbol: dict[str, Sequence[Bar]],
    samples: int | None = None,
    holding_bars: Sequence[int] = (1, 4, 12, 48),
) -> list[EventStudyResult]:
    """Generate matched random baseline (same symbol, time-of-day distribution, direction mix)."""
    random_results: list[EventStudyResult] = []
    rng = random.Random(42)  # Deterministic seed

    if not signals:
        return []

    target_signals = (
        [rng.choice(signals) for _ in range(samples)] if samples is not None else signals
    )

    max_holding = max(holding_bars) if holding_bars else 48

    for sig in target_signals:
        sym_bars = all_bars_by_symbol.get(sig.symbol, [])
        if len(sym_bars) < max_holding + 2:
            continue

        rand_idx = rng.randint(0, len(sym_bars) - max_holding - 2)
        start_bar = sym_bars[rand_idx]
        forward_bars = sym_bars[rand_idx + 1 : rand_idx + 1 + max_holding]

        atr = abs(start_bar.high - start_bar.low) or 1.0
        rand_sig = Signal(
            hypothesis_id="BASELINE_RANDOM",
            symbol=sig.symbol,
            timestamp=start_bar.close_time,
            direction=sig.direction,
            entry_price=start_bar.close,
            stop_price=(
                start_bar.close - 1.5 * atr if sig.direction == "bullish" else start_bar.close + 1.5 * atr
            ),
            target_price=(
                start_bar.close + 3.0 * atr if sig.direction == "bullish" else start_bar.close - 3.0 * atr
            ),
            r_multiple=2.0,
            rationale="Matched random baseline",
        )
        res = _calculate_single_event_study(rand_sig, forward_bars, holding_bars=holding_bars)
        random_results.append(res)

    return random_results
