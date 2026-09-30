"""Base class for strategy hypothesis plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.models import Signal


class HypothesisPlugin(ABC):
    @property
    @abstractmethod
    def hypothesis_id(self) -> str:
        """Unique hypothesis version string, e.g. 'H1_v1.0.0'."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Human readable name."""
        pass

    @abstractmethod
    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        """Evaluate point-in-time features and return a Signal if conditions met, else None."""
        pass
