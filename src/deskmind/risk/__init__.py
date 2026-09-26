"""Deterministic Risk Manager package for Deskmind.

Provides:
- RiskConfig: configuration with hard invariant caps
- Reason, ReasonCode: structured reason models for all checks
- IntentPayload, AccountState, MarketState: domain evaluation contexts
- RiskDecision: complete evaluation output
- evaluate: pure deterministic decision function
- CircuitBreakerTracker: stateful tracker for daily loss, drawdowns, and loss streaks
- RiskManagerSizer: adapter integrating with the backtest and execution engine
"""

from __future__ import annotations

from deskmind.risk.breakers import CircuitBreakerTracker, CircuitBreakerTransition
from deskmind.risk.config import (
    HARD_CAP_MAX_LEVERAGE,
    HARD_CAP_MAX_NOTIONAL_MULT,
    HARD_CAP_MAX_RISK_PCT,
    HARD_CAP_MAX_STOP_PRICE_PCT,
    RiskConfig,
)
from deskmind.risk.evaluator import evaluate
from deskmind.risk.models import (
    AccountState,
    IntentPayload,
    MarketState,
    Reason,
    ReasonCode,
    RiskDecision,
)
from deskmind.risk.sizer import RiskManagerSizer

__all__ = [
    "HARD_CAP_MAX_LEVERAGE",
    "HARD_CAP_MAX_NOTIONAL_MULT",
    "HARD_CAP_MAX_RISK_PCT",
    "HARD_CAP_MAX_STOP_PRICE_PCT",
    "AccountState",
    "CircuitBreakerTracker",
    "CircuitBreakerTransition",
    "IntentPayload",
    "MarketState",
    "Reason",
    "ReasonCode",
    "RiskConfig",
    "RiskDecision",
    "RiskManagerSizer",
    "evaluate",
]
