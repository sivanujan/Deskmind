"""Hypotheses package."""

from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal
from deskmind.research.hypotheses.registry import REGISTRY, HypothesisRegistry

__all__ = ["HypothesisPlugin", "Signal", "HypothesisRegistry", "REGISTRY"]
