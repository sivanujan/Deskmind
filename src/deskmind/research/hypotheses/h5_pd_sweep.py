"""H5 PDH/PDL Sweep Reversal hypothesis plugin."""

from __future__ import annotations

from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal, make_setup_id


class H5PDSweepPlugin(HypothesisPlugin):
    @property
    def hypothesis_id(self) -> str:
        return "H5_v1.0.0"

    @property
    def name(self) -> str:
        return "PDH/PDL Sweep Reversal"

    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        kz = str(r.get("killzone", "none"))
        if kz not in ("london_open", "ny_am"):
            return None

        pdh_sw = bool(r.get("pdh_swept_recent", False))
        pdl_sw = bool(r.get("pdl_swept_recent", False))
        ev_5m = str(r.get("last_event_5m", "none"))

        if not (pdh_sw or pdl_sw):
            return None

        atr = current_close * float(r.get("atr_pct", 0.01))
        if atr <= 0:
            atr = current_close * 0.01

        sym = str(r.get("symbol", "UNKNOWN"))
        ts = int(r.get("timestamp", 0))
        ev_confirmed = int(r.get("last_event_5m_confirmed_at") or ts)

        # PDL swept -> 5m CHoCH_up (Bullish reversal)
        if pdl_sw and ev_5m == "CHoCH_up":
            entry = current_close
            stop = entry - 1.0 * atr
            target = entry + 2.0 * atr
            setup_id = make_setup_id("H5", sym, ev_confirmed, "bullish")
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bullish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H5 Bullish: {kz} PDL sweep + 5m CHoCH_up",
                setup_id=setup_id,
            )

        # PDH swept -> 5m CHoCH_down (Bearish reversal)
        if pdh_sw and ev_5m == "CHoCH_down":
            entry = current_close
            stop = entry + 1.0 * atr
            target = entry - 2.0 * atr
            setup_id = make_setup_id("H5", sym, ev_confirmed, "bearish")
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bearish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H5 Bearish: {kz} PDH sweep + 5m CHoCH_down",
                setup_id=setup_id,
            )

        return None
