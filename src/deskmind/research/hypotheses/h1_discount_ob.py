"""H1 Discount OB / FVG Continuation hypothesis plugin."""

from __future__ import annotations

from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal, make_setup_id


class H1DiscountOBPlugin(HypothesisPlugin):
    @property
    def hypothesis_id(self) -> str:
        return "H1_v1.0.0"

    @property
    def name(self) -> str:
        return "Discount OB / FVG Continuation"

    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        t_1h = r.get("htf_trend_1h", "ranging")
        pd_zone = r.get("pd_zone_1h", "none")
        fvg_size = float(r.get("nearest_fvg_size_atr", 0.0))
        fvg_dist = float(r.get("nearest_fvg_dist_atr", 999.0))
        ob_state = str(r.get("nearest_ob_state", "none"))
        ob_dist = float(r.get("nearest_ob_dist_atr", 999.0))
        ev_5m = str(r.get("last_event_5m", "none"))

        atr = current_close * float(r.get("atr_pct", 0.01))
        if atr <= 0:
            atr = current_close * 0.01

        # Bullish setup
        sym = str(r.get("symbol", "UNKNOWN"))
        ts = int(r.get("timestamp", 0))
        ev_confirmed = int(r.get("last_event_5m_confirmed_at") or ts)

        if (
            t_1h == "bullish"
            and pd_zone == "discount"
            and ((fvg_dist <= 1.0 and fvg_size >= 0.5) or (ob_dist <= 1.0 and ob_state == "fresh"))
            and ev_5m == "CHoCH_up"
        ):
            entry = current_close
            stop = entry - 1.5 * atr
            target = entry + 3.0 * atr  # 2.0R target
            setup_id = make_setup_id("H1", sym, ev_confirmed, "bullish")
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bullish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale="H1 Bullish: 1h discount + fresh 15m POI + 5m CHoCH_up",
                setup_id=setup_id,
            )

        # Bearish setup
        if (
            t_1h == "bearish"
            and pd_zone == "premium"
            and ((fvg_dist <= 1.0 and fvg_size >= 0.5) or (ob_dist <= 1.0 and ob_state == "fresh"))
            and ev_5m == "CHoCH_down"
        ):
            entry = current_close
            stop = entry + 1.5 * atr
            target = entry - 3.0 * atr  # 2.0R target
            setup_id = make_setup_id("H1", sym, ev_confirmed, "bearish")
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bearish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale="H1 Bearish: 1h premium + fresh 15m POI + 5m CHoCH_down",
                setup_id=setup_id,
            )

        return None

