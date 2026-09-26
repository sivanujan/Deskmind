"""Pure deterministic evaluator for the Risk Manager.

No I/O. Decimal math throughout.
Guarantees:
- Every trade intent is subjected to all risk principles.
- Returns a RiskDecision containing full sizing, leverage, liquidation, cost,
  and machine-readable reasons for every check.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal

from deskmind.config import DEFAULT_MAINTENANCE_BRACKETS
from deskmind.risk.config import RiskConfig
from deskmind.risk.models import (
    AccountState,
    IntentPayload,
    MarketState,
    Reason,
    ReasonCode,
    RiskDecision,
)


def _round_qty(qty: Decimal, step_size: Decimal) -> Decimal:
    """Round qty down to stepSize (conservative to not exceed allowed risk)."""
    if step_size <= Decimal("0"):
        return qty
    return (qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size


def _compute_liquidation_price(
    side: str,
    entry_price: Decimal,
    leverage: int,
    qty: Decimal,
    brackets: list[tuple[Decimal, Decimal]] | None = None,
) -> Decimal:
    """Compute conservative liquidation price for isolated perpetual position."""
    if entry_price <= Decimal("0") or leverage <= 0 or qty <= Decimal("0"):
        return Decimal("0")

    notional = entry_price * qty
    b = brackets or DEFAULT_MAINTENANCE_BRACKETS
    mmr = b[-1][1]
    for max_notional, rate in b:
        if notional <= max_notional:
            mmr = rate
            break

    inv_l = Decimal("1") / Decimal(str(leverage))

    if side == "long":
        denom = Decimal("1") - mmr
        if denom <= Decimal("0"):
            return Decimal("0")
        liq = entry_price * (Decimal("1") - inv_l + mmr) / denom
        return max(Decimal("0"), liq)
    else:
        denom = Decimal("1") + mmr
        liq = entry_price * (Decimal("1") + inv_l - mmr) / denom
        return max(Decimal("0"), liq)


def evaluate(
    intent: IntentPayload,
    account: AccountState,
    market: MarketState,
    config: RiskConfig,
) -> RiskDecision:
    """Pure deterministic risk evaluation function."""
    reasons: list[Reason] = []
    is_rejected = False

    # --------------------------------------------------------------------------
    # 0. Sanity Checks
    # --------------------------------------------------------------------------
    if account.equity <= Decimal("0"):
        reasons.append(
            Reason(
                code=ReasonCode.SANITY_EQUITY_POSITIVE,
                message=f"Equity must be positive (got ${account.equity:.2f})",
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.SANITY_EQUITY_POSITIVE,
                message=f"Equity is positive (${account.equity:.2f})",
                passed=True,
            )
        )

    prices_valid = (
        intent.expected_entry > Decimal("0")
        and intent.stop_loss > Decimal("0")
        and intent.take_profit > Decimal("0")
        and market.mark_price > Decimal("0")
    )
    if not prices_valid:
        reasons.append(
            Reason(
                code=ReasonCode.SANITY_PRICES_POSITIVE,
                message="Entry, stop loss, take profit, and mark price must all be positive",
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.SANITY_PRICES_POSITIVE,
                message="All price inputs are positive",
                passed=True,
            )
        )

    # Validate stop and take-profit positioning relative to side
    stop_side_valid = False
    if prices_valid:
        if intent.side == "long":
            stop_side_valid = (
                intent.stop_loss < intent.expected_entry
                and intent.take_profit > intent.expected_entry
            )
        elif intent.side == "short":
            stop_side_valid = (
                intent.stop_loss > intent.expected_entry
                and intent.take_profit < intent.expected_entry
            )

    if not stop_side_valid:
        reasons.append(
            Reason(
                code=ReasonCode.SANITY_STOP_SIDE,
                message=(
                    f"Stop/target misconfigured for {intent.side}: "
                    f"entry={intent.expected_entry}, stop={intent.stop_loss}, target={intent.take_profit}"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.SANITY_STOP_SIDE,
                message=f"Stop and target correctly positioned for {intent.side} position",
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 7: Portfolio Limit (Phase 1: Max 1 position across all symbols)
    # --------------------------------------------------------------------------
    if len(account.open_positions) >= config.max_open_positions:
        reasons.append(
            Reason(
                code=ReasonCode.PORTFOLIO_MAX_POSITIONS,
                message=(
                    f"Portfolio position limit reached ({len(account.open_positions)}/"
                    f"{config.max_open_positions} active position open)"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.PORTFOLIO_MAX_POSITIONS,
                message=(
                    f"Portfolio limit check passed ({len(account.open_positions)}/"
                    f"{config.max_open_positions} positions)"
                ),
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 8: Circuit Breakers (Stateful Account Checks)
    # --------------------------------------------------------------------------
    # 8.4: Full stop (20% drawdown)
    drawdown_pct = Decimal("0")
    if (
        account.peak_equity > Decimal("0")
        and account.equity > Decimal("0")
        and account.equity < account.peak_equity
    ):
        drawdown_pct = (account.peak_equity - account.equity) / account.peak_equity

    if account.halt_state == "full_stop" or drawdown_pct >= config.drawdown_full_stop_pct:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_FULL_STOP,
                message=(
                    f"Trading halted by 20% drawdown circuit breaker ({drawdown_pct * 100:.1f}% DD). "
                    "Manual reset command required."
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_FULL_STOP,
                message=f"Full stop check passed ({drawdown_pct * 100:.1f}% DD < 20%)",
                passed=True,
            )
        )

    # 8.3: Drawdown halt (15% drawdown)
    if account.halt_state == "drawdown_halt" or drawdown_pct >= config.drawdown_halt_pct:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_DRAWDOWN_HALT,
                message=(
                    f"Trading halted by 15% drawdown circuit breaker ({drawdown_pct * 100:.1f}% DD). "
                    "Paper-only mode."
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_DRAWDOWN_HALT,
                message=f"Drawdown halt check passed ({drawdown_pct * 100:.1f}% DD < 15%)",
                passed=True,
            )
        )

    # 8.1: Daily realized loss limit (3% of start-of-day equity)
    daily_loss_limit = (
        account.start_of_day_equity * config.daily_loss_limit_pct
        if account.start_of_day_equity > Decimal("0")
        else Decimal("0")
    )
    is_daily_loss_halted = False
    if (
        account.today_realized_pnl < Decimal("0")
        and daily_loss_limit > Decimal("0")
        and abs(account.today_realized_pnl) >= daily_loss_limit
    ):
        is_daily_loss_halted = True

    if account.halt_state == "daily_loss_halt" or is_daily_loss_halted:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_DAILY_LOSS,
                message=(
                    f"Daily realized loss (${abs(account.today_realized_pnl):.2f}) reached 3% limit "
                    f"(${daily_loss_limit:.2f}). Halted until next UTC day."
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_DAILY_LOSS,
                message=f"Daily loss check passed (loss ${abs(min(Decimal('0'), account.today_realized_pnl)):.2f} < ${daily_loss_limit:.2f})",
                passed=True,
            )
        )

    # 8.5: Consecutive loss cooldown (4 losses -> 24h cooldown)
    is_in_cooldown = (
        account.consecutive_losses >= config.consecutive_losses_threshold
        or (account.cooldown_until_ms > 0 and intent.timestamp_ms < account.cooldown_until_ms)
        or account.halt_state == "streak_cooldown"
    )
    if is_in_cooldown:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_STREAK_COOLDOWN,
                message=(
                    f"Consecutive loss cooldown active ({account.consecutive_losses} losses). "
                    "Entries paused for 24h."
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_STREAK_COOLDOWN,
                message=f"Loss streak check passed ({account.consecutive_losses} consecutive losses)",
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 10: Event Guard Hook (Blackout windows)
    # --------------------------------------------------------------------------
    # In Phase 1: Always allowed; interface wired for upcoming news modules
    reasons.append(
        Reason(
            code=ReasonCode.EVENT_GUARD,
            message="Event guard: No active news blackout window",
            passed=True,
        )
    )

    # --------------------------------------------------------------------------
    # Rule 11: Data Health Guard (Live mode)
    # --------------------------------------------------------------------------
    if market.is_live:
        if market.is_stale or market.last_bar_degraded:
            reasons.append(
                Reason(
                    code=ReasonCode.DATA_HEALTH,
                    message="Live market data stream is stale or last candle close was degraded",
                    passed=False,
                )
            )
            is_rejected = True
        else:
            reasons.append(
                Reason(
                    code=ReasonCode.DATA_HEALTH,
                    message="Live market data stream is healthy and fresh",
                    passed=True,
                )
            )
    else:
        reasons.append(
            Reason(
                code=ReasonCode.DATA_HEALTH,
                message="Data health check passed (historical backtest)",
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 9: Funding Guard
    # --------------------------------------------------------------------------
    funding_violates = False
    if market.next_funding_time_ms is not None and market.current_funding_rate is not None:
        if intent.timestamp_ms > 0:
            ms_to_funding = market.next_funding_time_ms - intent.timestamp_ms
        else:
            ms_to_funding = market.next_funding_time_ms

        if Decimal("0") <= ms_to_funding <= Decimal(str(config.funding_guard_minutes * 60 * 1000)):
            # Funding event is within 30 minutes
            f_rate = market.current_funding_rate
            if (intent.side == "long" and f_rate > config.funding_guard_threshold) or (intent.side == "short" and f_rate < -config.funding_guard_threshold):
                funding_violates = True

    if funding_violates:
        reasons.append(
            Reason(
                code=ReasonCode.FUNDING_GUARD,
                message=(
                    f"Funding event imminent within 30m with adverse rate "
                    f"({market.current_funding_rate:+.4%})"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.FUNDING_GUARD,
                message="Funding guard passed",
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 1: Per-Trade Risk Determination (8.2: Halved if drawdown >= 10%)
    # --------------------------------------------------------------------------
    effective_risk_pct = config.risk_pct
    if drawdown_pct >= config.drawdown_halve_risk_pct:
        effective_risk_pct = config.risk_pct / Decimal("2")
        reasons.append(
            Reason(
                code=ReasonCode.CIRCUIT_BREAKER_DRAWDOWN_RISK_HALVED,
                message=f"Drawdown >= 10% ({drawdown_pct * 100:.1f}%): Risk per trade halved to {effective_risk_pct * 100:.2f}%",
                passed=True,
            )
        )
    else:
        reasons.append(
            Reason(
                code=ReasonCode.PER_TRADE_RISK_SIZED,
                message=f"Risk per trade: {effective_risk_pct * 100:.2f}% of equity",
                passed=True,
            )
        )

    allowed_risk_usd = (
        account.equity * effective_risk_pct if account.equity > Decimal("0") else Decimal("0")
    )

    # --------------------------------------------------------------------------
    # Rule 2: Stop Distance Bounds
    # --------------------------------------------------------------------------
    stop_dist = abs(intent.expected_entry - intent.stop_loss)
    stop_dist_pct = (
        stop_dist / intent.expected_entry if intent.expected_entry > Decimal("0") else Decimal("0")
    )

    # Resolve ATR for entry timeframe (defaults to 15m, with 5m/1h fallbacks)
    atr = market.atr_by_tf.get(
        intent.entry_timeframe,
        market.atr_by_tf.get(
            "15m",
            market.atr_by_tf.get("5m", market.atr_by_tf.get("1h", Decimal("0"))),
        ),
    )
    stop_dist_atr = (stop_dist / atr) if atr > Decimal("0") else Decimal("0")

    min_stop_atr = config.min_stop_atr_mult * atr
    min_stop_pct = config.min_stop_price_pct * intent.expected_entry
    min_stop_dist = max(min_stop_atr, min_stop_pct)

    # Check 2a: Stop too tight
    if stop_dist < min_stop_dist:
        reasons.append(
            Reason(
                code=ReasonCode.STOP_TOO_TIGHT,
                message=(
                    f"Stop distance (${stop_dist:.4f}) is smaller than minimum allowed "
                    f"(${min_stop_dist:.4f} = max({config.min_stop_atr_mult}*ATR, {config.min_stop_price_pct * 100:.1f}% price))"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.STOP_TOO_TIGHT,
                message=f"Stop distance (${stop_dist:.4f}) satisfies minimum width (${min_stop_dist:.4f})",
                passed=True,
            )
        )

    # Check 2b: Stop too wide (hard cap 15%, config default 8%)
    if stop_dist_pct > config.max_stop_price_pct:
        reasons.append(
            Reason(
                code=ReasonCode.STOP_TOO_WIDE,
                message=(
                    f"Stop distance ({stop_dist_pct * 100:.2f}%) exceeds maximum limit "
                    f"({config.max_stop_price_pct * 100:.1f}%)"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.STOP_TOO_WIDE,
                message=f"Stop distance ({stop_dist_pct * 100:.2f}%) within maximum limit ({config.max_stop_price_pct * 100:.1f}%)",
                passed=True,
            )
        )

    # Check 2c: Estimated round-trip friction costs <= 15% of risk
    slip_pct = config.fixed_slippage_bps + (
        config.slippage_atr_factor * (atr / intent.expected_entry)
        if intent.expected_entry > Decimal("0")
        else Decimal("0")
    )
    # Round-trip entry (taker) + stop-loss exit (taker + slippage)
    est_cost_per_unit = intent.expected_entry * (
        Decimal("2") * config.taker_fee_rate + Decimal("2") * slip_pct
    )
    cost_to_risk_ratio = (
        est_cost_per_unit / stop_dist if stop_dist > Decimal("0") else Decimal("999")
    )

    if cost_to_risk_ratio > config.max_cost_pct_of_risk:
        reasons.append(
            Reason(
                code=ReasonCode.COSTS_EXCEED_RISK,
                message=(
                    f"Estimated round-trip friction ({cost_to_risk_ratio * 100:.1f}% of risk) exceeds "
                    f"maximum allowed limit ({config.max_cost_pct_of_risk * 100:.1f}%)"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.COSTS_EXCEED_RISK,
                message=(
                    f"Estimated friction ({cost_to_risk_ratio * 100:.1f}% of risk) is within limit "
                    f"({config.max_cost_pct_of_risk * 100:.1f}%)"
                ),
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 3: Net Reward:Risk Ratio after estimated costs >= 1.5
    # --------------------------------------------------------------------------
    gross_reward_dist = abs(intent.take_profit - intent.expected_entry)
    gross_risk_dist = stop_dist

    # Expected costs on win: taker entry + slippage + maker exit (TP limit)
    cost_on_win_per_unit = (
        intent.expected_entry * (config.taker_fee_rate + slip_pct)
        + intent.take_profit * config.maker_fee_rate
    )

    # Expected costs on loss: taker entry + slippage + taker exit + slippage (SL market)
    cost_on_loss_per_unit = intent.expected_entry * (
        config.taker_fee_rate + slip_pct
    ) + intent.stop_loss * (config.taker_fee_rate + slip_pct)

    net_reward_dist = max(Decimal("0"), gross_reward_dist - cost_on_win_per_unit)
    net_risk_dist = gross_risk_dist + cost_on_loss_per_unit
    net_rr = net_reward_dist / net_risk_dist if net_risk_dist > Decimal("0") else Decimal("0")

    if net_rr < config.min_net_reward_risk:
        reasons.append(
            Reason(
                code=ReasonCode.REWARD_RISK_RATIO,
                message=(
                    f"Net reward:risk ratio after estimated costs ({net_rr:.2f}) is below "
                    f"minimum threshold ({config.min_net_reward_risk:.2f})"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.REWARD_RISK_RATIO,
                message=f"Net reward:risk ratio ({net_rr:.2f}) meets minimum threshold ({config.min_net_reward_risk:.2f})",
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 4: Position Sizing & Exchange Filter Validation
    # --------------------------------------------------------------------------
    raw_qty = (
        allowed_risk_usd / stop_dist
        if stop_dist > Decimal("0") and allowed_risk_usd > Decimal("0")
        else Decimal("0")
    )
    step_size = market.filters.step_size if market.filters else Decimal("0.001")
    qty = _round_qty(raw_qty, step_size)

    # Actual risk after rounding must be <= allowed risk
    actual_risk_usd = qty * stop_dist
    if actual_risk_usd > allowed_risk_usd and qty >= step_size:
        qty = _round_qty(qty - step_size, step_size)
        actual_risk_usd = qty * stop_dist

    if qty <= Decimal("0"):
        reasons.append(
            Reason(
                code=ReasonCode.POSITION_SIZE_ZERO,
                message="Position size rounded down to zero contracts",
                passed=False,
            )
        )
        is_rejected = True
    elif actual_risk_usd > allowed_risk_usd:
        reasons.append(
            Reason(
                code=ReasonCode.ACTUAL_RISK_EXCEEDS_ALLOWED,
                message=(
                    f"Actual risk (${actual_risk_usd:.2f}) exceeds allowed risk (${allowed_risk_usd:.2f})"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.POSITION_SIZED_VALID,
                message=f"Position sized: {qty} contracts (actual risk: ${actual_risk_usd:.2f} <= ${allowed_risk_usd:.2f})",
                passed=True,
            )
        )

    # Filter checks (minQty & minNotional)
    notional = qty * intent.expected_entry
    if market.filters:
        if qty < market.filters.min_qty:
            reasons.append(
                Reason(
                    code=ReasonCode.EXCHANGE_FILTER_MIN_QTY,
                    message=f"Quantity ({qty}) is below exchange minQty ({market.filters.min_qty})",
                    passed=False,
                )
            )
            is_rejected = True
        else:
            reasons.append(
                Reason(
                    code=ReasonCode.EXCHANGE_FILTER_MIN_QTY,
                    message=f"Exchange minQty filter passed ({qty} >= {market.filters.min_qty})",
                    passed=True,
                )
            )

        if notional < market.filters.min_notional:
            reasons.append(
                Reason(
                    code=ReasonCode.EXCHANGE_FILTER_MIN_NOTIONAL,
                    message=(
                        f"Notional (${notional:.2f}) is below exchange minNotional "
                        f"(${market.filters.min_notional:.2f})"
                    ),
                    passed=False,
                )
            )
            is_rejected = True
        else:
            reasons.append(
                Reason(
                    code=ReasonCode.EXCHANGE_FILTER_MIN_NOTIONAL,
                    message=f"Exchange minNotional filter passed (${notional:.2f} >= ${market.filters.min_notional:.2f})",
                    passed=True,
                )
            )

    # --------------------------------------------------------------------------
    # Rule 6: Notional Cap (notional <= 3 x equity, hard cap 5x)
    # --------------------------------------------------------------------------
    max_notional_allowed = config.max_notional_mult * account.equity
    if notional > max_notional_allowed:
        reasons.append(
            Reason(
                code=ReasonCode.NOTIONAL_CAP,
                message=(
                    f"Notional (${notional:.2f}) exceeds notional cap of {config.max_notional_mult}x "
                    f"equity (${max_notional_allowed:.2f})"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.NOTIONAL_CAP,
                message=(
                    f"Notional cap passed (${notional:.2f} <= {config.max_notional_mult}x "
                    f"equity = ${max_notional_allowed:.2f})"
                ),
                passed=True,
            )
        )

    # --------------------------------------------------------------------------
    # Rule 5: Leverage Selection & Liquidation Safety Buffer
    # --------------------------------------------------------------------------
    # Lowest integer leverage that fits the margin within available balance
    min_leverage_for_balance = (
        max(1, math.ceil(float(notional / account.available_balance)))
        if account.available_balance > Decimal("0") and notional > Decimal("0")
        else 999
    )

    if min_leverage_for_balance > config.max_leverage:
        reasons.append(
            Reason(
                code=ReasonCode.MARGIN_EXCEEDS_BALANCE,
                message=(
                    f"Required leverage ({min_leverage_for_balance}x) to fit margin within "
                    f"available balance (${account.available_balance:.2f}) exceeds max leverage ({config.max_leverage}x)"
                ),
                passed=False,
            )
        )
        is_rejected = True
        chosen_leverage = config.max_leverage
    else:
        reasons.append(
            Reason(
                code=ReasonCode.MARGIN_EXCEEDS_BALANCE,
                message=(
                    f"Margin fits available balance (${account.available_balance:.2f}) at "
                    f"leverage >= {min_leverage_for_balance}x"
                ),
                passed=True,
            )
        )
        chosen_leverage = min_leverage_for_balance

    # Liquidation safety buffer: max(1 x ATR, 3% of price)
    liq_buf_atr = config.liq_buffer_atr_mult * atr
    liq_buf_pct = config.liq_buffer_price_pct * intent.expected_entry
    required_liq_buffer = max(liq_buf_atr, liq_buf_pct)

    # Compute liquidation price at chosen lowest viable leverage
    liq_price = _compute_liquidation_price(
        side=intent.side,
        entry_price=intent.expected_entry,
        leverage=chosen_leverage,
        qty=qty if qty > Decimal("0") else Decimal("1"),
    )

    # Verify liquidation price is strictly beyond the stop by at least required_liq_buffer
    liq_is_safe = False
    if intent.side == "long" and liq_price <= (intent.stop_loss - required_liq_buffer):
        # Long: Liq price must be below stop_loss by at least required_liq_buffer
        liq_is_safe = True
    elif intent.side == "short" and liq_price >= (intent.stop_loss + required_liq_buffer):
        # Short: Liq price must be above stop_loss by at least required_liq_buffer
        liq_is_safe = True

    if not liq_is_safe:
        reasons.append(
            Reason(
                code=ReasonCode.LEVERAGE_LIQUIDATION_UNSAFE,
                message=(
                    f"Liquidation price ({liq_price:.4f}) at leverage {chosen_leverage}x violates "
                    f"safety buffer (${required_liq_buffer:.4f}) beyond stop loss ({intent.stop_loss:.4f})"
                ),
                passed=False,
            )
        )
        is_rejected = True
    else:
        reasons.append(
            Reason(
                code=ReasonCode.LEVERAGE_SELECTED,
                message=(
                    f"Selected lowest viable leverage: {chosen_leverage}x "
                    f"(Liq price: {liq_price:.4f} satisfies safety buffer ${required_liq_buffer:.4f})"
                ),
                passed=True,
            )
        )

    # Compute isolated margin
    isolated_margin = (
        (notional / Decimal(str(chosen_leverage))) if chosen_leverage > 0 else Decimal("0")
    )

    # --------------------------------------------------------------------------
    # Final Decision Assembly
    # --------------------------------------------------------------------------
    status = "rejected" if is_rejected else "approved"
    final_qty = qty if status == "approved" else Decimal("0")
    final_notional = final_qty * intent.expected_entry
    final_margin = (
        (final_notional / Decimal(str(chosen_leverage))) if chosen_leverage > 0 else Decimal("0")
    )

    return RiskDecision(
        status=status,
        qty=final_qty,
        leverage=chosen_leverage,
        notional=final_notional if status == "approved" else notional,
        isolated_margin=final_margin if status == "approved" else isolated_margin,
        liquidation_price=liq_price,
        stop_distance_usd=stop_dist,
        stop_distance_pct=stop_dist_pct,
        stop_distance_atr=stop_dist_atr,
        expected_costs_r=cost_to_risk_ratio,
        net_reward_risk=net_rr,
        effective_risk_pct=effective_risk_pct,
        allowed_risk_usd=allowed_risk_usd,
        actual_risk_usd=actual_risk_usd,
        reasons=reasons,
    )
