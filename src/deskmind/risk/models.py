"""Domain models and data structures for the Risk Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from deskmind.backtest.filters import SymbolFilters


class ReasonCode:
    """Standardized machine-readable reason codes for all risk checks."""

    # Sanity checks
    SANITY_EQUITY_POSITIVE = "SANITY_EQUITY_POSITIVE"
    SANITY_PRICES_POSITIVE = "SANITY_PRICES_POSITIVE"
    SANITY_STOP_SIDE = "SANITY_STOP_SIDE"

    # Rule 7: Portfolio limits
    PORTFOLIO_MAX_POSITIONS = "PORTFOLIO_MAX_POSITIONS"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"  # Engine-level, before RM

    # Engine-level: duplicate setup deduplication (before RM)
    DUPLICATE_SETUP_ID = "DUPLICATE_SETUP_ID"

    # Rule 8: Circuit breakers
    CIRCUIT_BREAKER_FULL_STOP = "CIRCUIT_BREAKER_FULL_STOP"
    CIRCUIT_BREAKER_DRAWDOWN_HALT = "CIRCUIT_BREAKER_DRAWDOWN_HALT"
    CIRCUIT_BREAKER_DAILY_LOSS = "CIRCUIT_BREAKER_DAILY_LOSS"
    CIRCUIT_BREAKER_STREAK_COOLDOWN = "CIRCUIT_BREAKER_STREAK_COOLDOWN"
    CIRCUIT_BREAKER_DRAWDOWN_RISK_HALVED = "CIRCUIT_BREAKER_DRAWDOWN_RISK_HALVED"

    # Rule 10: Event guard
    EVENT_GUARD = "EVENT_GUARD"

    # Rule 11: Data health
    DATA_HEALTH = "DATA_HEALTH"

    # Rule 9: Funding guard
    FUNDING_GUARD = "FUNDING_GUARD"

    # Rule 1: Risk determination
    PER_TRADE_RISK_SIZED = "PER_TRADE_RISK_SIZED"

    # Rule 2: Stop distance bounds
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    COSTS_EXCEED_RISK = "COSTS_EXCEED_RISK"
    STOP_DISTANCE_VALID = "STOP_DISTANCE_VALID"

    # Rule 3: Net reward:risk
    REWARD_RISK_RATIO = "REWARD_RISK_RATIO"

    # Rule 4: Sizing & Exchange filters
    POSITION_SIZE_ZERO = "POSITION_SIZE_ZERO"
    ACTUAL_RISK_EXCEEDS_ALLOWED = "ACTUAL_RISK_EXCEEDS_ALLOWED"
    EXCHANGE_FILTER_MIN_QTY = "EXCHANGE_FILTER_MIN_QTY"
    EXCHANGE_FILTER_MIN_NOTIONAL = "EXCHANGE_FILTER_MIN_NOTIONAL"
    POSITION_SIZED_VALID = "POSITION_SIZED_VALID"

    # Rule 6: Notional cap
    NOTIONAL_CAP = "NOTIONAL_CAP"

    # Rule 5: Leverage & Liquidation safety
    MARGIN_EXCEEDS_BALANCE = "MARGIN_EXCEEDS_BALANCE"
    LEVERAGE_LIQUIDATION_UNSAFE = "LEVERAGE_LIQUIDATION_UNSAFE"
    LEVERAGE_SELECTED = "LEVERAGE_SELECTED"


@dataclass(frozen=True)
class Reason:
    """An individual check result evaluated by the Risk Manager."""

    code: str
    message: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "passed": self.passed,
        }


@dataclass
class IntentPayload:
    """Trade intent supplied to the Risk Manager."""

    symbol: str
    side: Literal["long", "short"]
    expected_entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    strategy_name: str = "unknown"
    strategy_version: str = "1.0.0"
    entry_timeframe: str = "15m"
    timestamp_ms: int = 0


@dataclass
class AccountState:
    """Account and balance context provided to the Risk Manager."""

    equity: Decimal
    available_balance: Decimal
    open_positions: list[str] = field(default_factory=list)
    today_realized_pnl: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    consecutive_losses: int = 0
    halt_state: str = "normal"  # normal, daily_loss_halt, drawdown_halt, full_stop, streak_cooldown
    start_of_day_equity: Decimal = Decimal("0")
    cooldown_until_ms: int = 0
    is_risk_halved: bool | None = None
    drawdown_halt_entered_ms: int = 0


@dataclass
class MarketState:
    """Current market conditions provided to the Risk Manager."""

    mark_price: Decimal
    atr_by_tf: dict[str, Decimal] = field(default_factory=dict)
    next_funding_time_ms: int | None = None
    current_funding_rate: Decimal | None = None
    filters: SymbolFilters | None = None
    is_live: bool = False
    is_stale: bool = False
    last_bar_degraded: bool = False


@dataclass
class RiskDecision:
    """Deterministic decision emitted by the Risk Manager.

    Designed to fit cleanly into trade_journal.risk_decision (JSONB).
    """

    status: Literal["approved", "rejected"]
    qty: Decimal
    leverage: int
    notional: Decimal
    isolated_margin: Decimal
    liquidation_price: Decimal
    stop_distance_usd: Decimal
    stop_distance_pct: Decimal
    stop_distance_atr: Decimal
    expected_costs_r: Decimal
    net_reward_risk: Decimal
    effective_risk_pct: Decimal
    allowed_risk_usd: Decimal
    actual_risk_usd: Decimal
    reasons: list[Reason] = field(default_factory=list)

    @property
    def passed_reasons(self) -> list[Reason]:
        return [r for r in self.reasons if r.passed]

    @property
    def failed_reasons(self) -> list[Reason]:
        return [r for r in self.reasons if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON/JSONB storage."""
        return {
            "status": self.status,
            "qty": str(self.qty),
            "leverage": self.leverage,
            "notional": float(self.notional),
            "isolated_margin": float(self.isolated_margin),
            "liquidation_price": float(self.liquidation_price),
            "stop_distance_usd": float(self.stop_distance_usd),
            "stop_distance_pct": float(self.stop_distance_pct),
            "stop_distance_atr": float(self.stop_distance_atr),
            "expected_costs_r": float(self.expected_costs_r),
            "net_reward_risk": float(self.net_reward_risk),
            "effective_risk_pct": float(self.effective_risk_pct),
            "allowed_risk_usd": float(self.allowed_risk_usd),
            "actual_risk_usd": float(self.actual_risk_usd),
            "reasons": [r.to_dict() for r in self.reasons],
            "failed_reason_codes": [r.code for r in self.failed_reasons],
        }
