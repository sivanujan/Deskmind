"""Registry of pre-registered research hypotheses and version/trial tracker."""

from __future__ import annotations

from typing import Sequence

from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin


class HypothesisRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, HypothesisPlugin] = {
            "H1_v1.0.0": H1DiscountOBPlugin(),
            "H2_v1.0.0": H2SweepReversalPlugin(),
            "H3_v1.0.0": H3SilverBulletPlugin(),
            "H4_v1.0.0": H4BreakoutRetestPlugin(),
            "H5_v1.0.0": H5PDSweepPlugin(),
        }
        self._trial_counts: dict[str, int] = {hid: 0 for hid in self._plugins}

    def list_hypotheses(self) -> Sequence[HypothesisPlugin]:
        return list(self._plugins.values())

    def get_plugin(self, hypothesis_id: str) -> HypothesisPlugin | None:
        return self._plugins.get(hypothesis_id)

    def record_trial(self, hypothesis_id: str) -> int:
        """Increment and return trial count for a given hypothesis version."""
        if hypothesis_id in self._trial_counts:
            self._trial_counts[hypothesis_id] += 1
            return self._trial_counts[hypothesis_id]
        self._trial_counts[hypothesis_id] = 1
        return 1

    def get_trial_count(self, hypothesis_id: str) -> int:
        return self._trial_counts.get(hypothesis_id, 0)


REGISTRY = HypothesisRegistry()
