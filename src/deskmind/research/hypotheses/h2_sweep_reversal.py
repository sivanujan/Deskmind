"""H2 Liquidity Sweep Reversal hypothesis plugin."""

from __future__ import annotations

from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal


class H2SweepReversalPlugin(HypothesisPlugin):
    @property
    def hypothesis_id(self) -> str:
        return "H2_v1.0.0"

    @property
    def name(self) -> str:
        return "Liquidity Sweep Reversal"

    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        kz = str(r.get("killzone", "none"))
        sweep_side = str(r.get("strong_pool_swept_side", "none"))
        sweep_bars = int(r.get("strong_pool_swept_bars_ago", -1))
        ev_5m = str(r.get("last_event_5m", "none"))

        if kz not in ("london_open", "ny_am"):
            return None

        atr = current_close * float(r.get("atr_pct", 0.01))
        if atr <= 0:
            atr = current_close * 0.01

        sym = str(r.get("symbol", "UNKNOWN"))
        ts = int(r.get("timestamp", 0))
        ev_confirmed = int(r.get("last_event_5m_confirmed_at") or ts)

        # Bullish: SSL swept + 5m CHoCH_up within 12 bars
        if sweep_side == "SSL" and 0 <= sweep_bars <= 12 and ev_5m == "CHoCH_up":
            entry = current_close
            stop = entry - 1.2 * atr
            target = entry + 2.4 * atr  # 2.0R target to opposite pool
            setup_id = f"{sym}:H2:{ev_confirmed}:bullish"
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bullish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H2 Bullish: {kz} SSL sweep + 5m CHoCH_up",
                setup_id=setup_id,
            )

        # Bearish: BSL swept + 5m CHoCH_down within 12 bars
        if sweep_side == "BSL" and 0 <= sweep_bars <= 12 and ev_5m == "CHoCH_down":
            entry = current_close
            stop = entry + 1.2 * atr
            target = entry - 2.4 * atr
            setup_id = f"{sym}:H2:{ev_confirmed}:bearish"
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bearish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H2 Bearish: {kz} BSL sweep + 5m CHoCH_down",
                setup_id=setup_id,
            )

        return None
