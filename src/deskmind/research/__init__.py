"""Deskmind Research Framework package."""

from deskmind.research.event_study import (
    EventStudyResult,
    calculate_event_study,
    generate_matched_random_baseline,
)
from deskmind.research.gates import GateCheckResult, calculate_deflated_sharpe, evaluate_research_gates

__all__ = [
    "EventStudyResult",
    "calculate_event_study",
    "generate_matched_random_baseline",
    "GateCheckResult",
    "evaluate_research_gates",
    "calculate_deflated_sharpe",
]
