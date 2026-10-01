"""H4 Support/Resistance Breakout-Retest hypothesis plugin."""

from __future__ import annotations

from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal, make_setup_id


class H4BreakoutRetestPlugin(HypothesisPlugin):
    @property
    def hypothesis_id(self) -> str:
        return "H4_v1.1.0"

    @property
    def name(self) -> str:
        return "Support/Resistance Breakout-Retest"

    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        t_1h = r.get("htf_trend_1h", "ranging")
        sr_touches = int(r.get("nearest_sr_touches", 0))
        sr_dist = float(r.get("nearest_sr_dist_atr", 999.0))
        at_zone = bool(r.get("pattern_recent_at_zone", False))
        pat_trend = str(r.get("pattern_recent_with_trend", "none"))

        if not (sr_touches >= 3 and sr_dist <= 0.5 and at_zone and pat_trend == "aligned"):
            return None

        atr = current_close * float(r.get("atr_pct", 0.01))
        if atr <= 0:
            atr = current_close * 0.01

        sym = str(r.get("symbol", "UNKNOWN"))
        ts = int(r.get("timestamp", 0))
        pat_bars_ago = int(r.get("pattern_recent_bars_ago", 0))
        pat_confirmed = ts - (pat_bars_ago * 300_000)

        if t_1h == "bullish":
            entry = current_close
            stop = entry - 1.2 * atr
            target = entry + 2.4 * atr  # Fixed 2.0R target
            setup_id = make_setup_id("H4", sym, pat_confirmed, "bullish")
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bullish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H4 Bullish: S/R touches {sr_touches} breakout retest holding in 1h trend",
                setup_id=setup_id,
            )

        if t_1h == "bearish":
            entry = current_close
            stop = entry + 1.2 * atr
            target = entry - 2.4 * atr
            setup_id = make_setup_id("H4", sym, pat_confirmed, "bearish")
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bearish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H4 Bearish: S/R touches {sr_touches} breakout retest holding in 1h trend",
                setup_id=setup_id,
            )

        return None
