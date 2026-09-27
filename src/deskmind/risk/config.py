"""Risk Manager configuration and hard safety caps.

All monetary, risk and price percentages are modeled with Decimal.
Hard caps are code-level constants that cannot be exceeded by configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# ------------------------------------------------------------------------------
# Hard Safety Caps (Code constants that cannot be bypassed or exceeded)
# ------------------------------------------------------------------------------
HARD_CAP_MAX_RISK_PCT: Decimal = Decimal("0.02")  # Maximum 2% risk of equity per trade
HARD_CAP_MAX_STOP_PRICE_PCT: Decimal = Decimal("0.15")  # Maximum 15% stop distance from price
HARD_CAP_MAX_LEVERAGE: int = 10  # Maximum 10x leverage
HARD_CAP_MAX_NOTIONAL_MULT: Decimal = Decimal("5.0")  # Maximum 5x equity notional exposure


@dataclass(frozen=True)
class RiskConfig:
    """Configurable risk parameters with strict validation against hard caps."""

    # Rule 1: Per-trade risk
    risk_pct: Decimal = Decimal("0.01")  # Default 1% equity risk

    # Rule 2: Stop distance bounds
    min_stop_atr_mult: Decimal = Decimal("0.5")  # Min 0.5 x ATR(entry timeframe)
    min_stop_price_pct: Decimal = Decimal("0.003")  # Min 0.3% of entry price
    max_cost_pct_of_risk: Decimal = Decimal("0.15")  # Max 15% estimated friction of risk
    max_stop_price_pct: Decimal = Decimal("0.08")  # Max 8% of price (hard cap 15%)

    # Rule 3: Net reward:risk
    min_net_reward_risk: Decimal = Decimal("1.5")  # Min 1.5 net reward:risk ratio

    # Rule 5: Leverage & Liquidation safety
    max_leverage: int = 5  # Max 5x leverage (hard cap 10x)
    liq_buffer_atr_mult: Decimal = Decimal("1.0")  # Min 1.0 x ATR beyond stop
    liq_buffer_price_pct: Decimal = Decimal("0.03")  # Min 3.0% price beyond stop

    # Rule 6: Notional cap
    max_notional_mult: Decimal = Decimal("3.0")  # Max 3x equity notional (hard cap 5x)

    # Rule 7: Portfolio limits (Phase 1)
    max_open_positions: int = 1  # Exactly 1 position across all symbols

    # Rule 8: Circuit breakers
    enable_circuit_breakers: bool = True
    daily_loss_limit_pct: Decimal = Decimal("0.03")  # 3% of start-of-day equity
    drawdown_halve_risk_pct: Decimal = Decimal("0.10")  # 10% drawdown -> halve risk
    drawdown_halve_risk_recovery_pct: Decimal = Decimal("0.07")  # 7% recovery (hysteresis)
    drawdown_halt_pct: Decimal = Decimal("0.15")  # 15% drawdown -> halt new entries
    drawdown_halt_recovery_pct: Decimal = Decimal("0.10")  # 10% recovery threshold (hysteresis)
    drawdown_halt_min_days: int = 7  # minimum 7 days before drawdown halt clears
    drawdown_full_stop_pct: Decimal = Decimal("0.20")  # 20% drawdown -> full stop
    consecutive_losses_threshold: int = 4  # 4 consecutive losses -> cooldown
    consecutive_losses_cooldown_hours: int = 24  # 24 hour cooldown

    # Rule 9: Funding guard
    funding_guard_minutes: int = 30  # Funding event within 30 minutes
    funding_guard_threshold: Decimal = Decimal("0.0003")  # 0.03% adverse funding rate

    # Cost model parameters (Binance VIP0 standard)
    maker_fee_rate: Decimal = Decimal("0.0002")  # 0.02%
    taker_fee_rate: Decimal = Decimal("0.0005")  # 0.05%
    fixed_slippage_bps: Decimal = Decimal("0.0002")  # 2 bps
    slippage_atr_factor: Decimal = Decimal("0.05")  # 0.05 ATR/price
    liquidation_fee_rate: Decimal = Decimal("0.0125")  # 1.25% Binance clearance fee

    def __post_init__(self) -> None:
        """Validate all parameters against invariant hard caps."""
        if self.risk_pct <= Decimal("0"):
            raise ValueError(f"risk_pct must be positive, got {self.risk_pct}")
        if self.risk_pct > HARD_CAP_MAX_RISK_PCT:
            raise ValueError(
                f"risk_pct {self.risk_pct} exceeds hard cap of {HARD_CAP_MAX_RISK_PCT}"
            )

        if self.max_stop_price_pct <= Decimal("0"):
            raise ValueError(f"max_stop_price_pct must be positive, got {self.max_stop_price_pct}")
        if self.max_stop_price_pct > HARD_CAP_MAX_STOP_PRICE_PCT:
            raise ValueError(
                f"max_stop_price_pct {self.max_stop_price_pct} exceeds hard cap of "
                f"{HARD_CAP_MAX_STOP_PRICE_PCT}"
            )

        if self.max_leverage <= 0:
            raise ValueError(f"max_leverage must be positive, got {self.max_leverage}")
        if self.max_leverage > HARD_CAP_MAX_LEVERAGE:
            raise ValueError(
                f"max_leverage {self.max_leverage} exceeds hard cap of {HARD_CAP_MAX_LEVERAGE}"
            )

        if self.max_notional_mult <= Decimal("0"):
            raise ValueError(f"max_notional_mult must be positive, got {self.max_notional_mult}")
        if self.max_notional_mult > HARD_CAP_MAX_NOTIONAL_MULT:
            raise ValueError(
                f"max_notional_mult {self.max_notional_mult} exceeds hard cap of "
                f"{HARD_CAP_MAX_NOTIONAL_MULT}"
            )
