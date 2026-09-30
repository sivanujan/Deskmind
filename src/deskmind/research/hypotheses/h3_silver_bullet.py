"""H3 Silver Bullet Window FVG hypothesis plugin."""

from __future__ import annotations

from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal


class H3SilverBulletPlugin(HypothesisPlugin):
    @property
    def hypothesis_id(self) -> str:
        return "H3_v1.0.0"

    @property
    def name(self) -> str:
        return "Silver Bullet Window FVG"

    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        sb_active = bool(r.get("silver_bullet_active", False))
        if not sb_active:
            return None

        t_1h = r.get("htf_trend_1h", "ranging")
        fvg_disp = bool(r.get("nearest_fvg_displacement", False))
        fvg_dist = float(r.get("nearest_fvg_dist_atr", 999.0))
        fvg_size = float(r.get("nearest_fvg_size_atr", 0.0))

        if not (fvg_disp and fvg_dist <= 1.0 and fvg_size >= 0.25):
            return None

        atr = current_close * float(r.get("atr_pct", 0.01))
        if atr <= 0:
            atr = current_close * 0.01

        if t_1h == "bullish":
            entry = current_close
            stop = entry - 1.0 * atr
            target = entry + 2.0 * atr  # 2.0R target
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=str(r.get("symbol", "UNKNOWN")),
                timestamp=int(r.get("timestamp", 0)),
                direction="bullish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale="H3 Bullish: Silver Bullet window displacement FVG in 1h uptrend",
            )

        if t_1h == "bearish":
            entry = current_close
            stop = entry + 1.0 * atr
            target = entry - 2.0 * atr
            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=str(r.get("symbol", "UNKNOWN")),
                timestamp=int(r.get("timestamp", 0)),
                direction="bearish",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale="H3 Bearish: Silver Bullet window displacement FVG in 1h downtrend",
            )

        return None
