# Deskmind Risk Manager Architecture & Specification

## 1. Principles & Philosophy

The Deskmind Risk Manager is the final, uncompromising gatekeeper of capital. It enforces strict, deterministic risk rules derived from quantitative first principles, not from optimizing backtest curve-fits.

### Core Invariants
1. **Identical Code Path**: Backtesting and live trading run through the exact same code (`deskmind.risk.evaluate()`).
2. **Pure Deterministic Function**: `evaluate(intent, account, market, config) -> RiskDecision`. Same inputs **always** yield the exact same decision. Zero I/O, zero network calls, zero non-deterministic random calls inside the evaluator.
3. **Strict Decimal Arithmetic**: No floating-point rounding ambiguities. All currency, risk, fee, and price calculations use Python's `Decimal` type.
4. **Veto is Final**: If the Risk Manager rejects an intent, nothing downstream can override the rejection.
5. **Hard Safety Caps**: Hard caps are hardcoded constants in Python code (`HARD_CAP_*`) that cannot be bypassed or exceeded, even if configuration attempts to supply larger values.

---

## 2. Interface Contract

### Function Signature
```python
def evaluate(
    intent: IntentPayload,
    account: AccountState,
    market: MarketState,
    config: RiskConfig,
) -> RiskDecision: ...
```

### Data Structures

#### `IntentPayload`
- `symbol`: Trading pair (e.g., `"BTCUSDT"`).
- `side`: `"long"` or `"short"`.
- `expected_entry`: Planned fill price ($).
- `stop_loss`: Initial protective stop loss level ($).
- `take_profit`: Target exit price ($).
- `strategy_name`: Identifier of the generating strategy.
- `strategy_version`: Semver of the strategy logic.
- `entry_timeframe`: Base timeframe (e.g., `"15m"`).
- `timestamp_ms`: Event timestamp in UTC milliseconds.

#### `AccountState`
- `equity`: Total account equity ($).
- `available_balance`: Free cash available for margin ($).
- `open_positions`: List of symbols with an active position.
- `today_realized_pnl`: Realized profit/loss since 00:00:00 UTC ($).
- `peak_equity`: Highest historical equity recorded ($).
- `consecutive_losses`: Current count of consecutive losing trades.
- `halt_state`: Current circuit breaker state (`"normal"`, `"daily_loss_halt"`, `"drawdown_halt"`, `"full_stop"`, `"streak_cooldown"`).
- `start_of_day_equity`: Equity at 00:00:00 UTC start-of-day.
- `cooldown_until_ms`: UTC millisecond timestamp when streak cooldown expires.

#### `MarketState`
- `mark_price`: Current mark price ($).
- `atr_by_tf`: ATR map by timeframe (e.g., `{"15m": Decimal("400.0")}`).
- `next_funding_time_ms`: UTC millisecond timestamp of the next funding rate event.
- `current_funding_rate`: Latest funding rate (e.g., `Decimal("0.0001")` = +0.01%).
- `filters`: Exchange rules (`step_size`, `min_qty`, `max_qty`, `min_notional`, `tick_size`).
- `is_live`: Boolean flag indicating live vs backtest environment.
- `is_stale`: Data staleness indicator for live mode.
- `last_bar_degraded`: Live data quality indicator.

#### `RiskDecision`
- `status`: `"approved"` or `"rejected"`.
- `qty`: Sized order quantity (contracts), rounded DOWN to `step_size`.
- `leverage`: Selected isolated leverage ($1\times$ to $5\times$, hard cap $10\times$).
- `notional`: Total position notional ($ = \text{qty} \times \text{entry}$).
- `isolated_margin`: Initial margin required ($ = \text{notional} / \text{leverage}$).
- `liquidation_price`: Estimated liquidation price based on Binance MMR brackets.
- `stop_distance_usd`, `stop_distance_pct`, `stop_distance_atr`: Distance to stop loss.
- `expected_costs_r`: Estimated round-trip friction in units of R.
- `net_reward_risk`: Effective reward-to-risk ratio after transaction costs.
- `allowed_risk_usd`: Maximum allowed risk capital for the trade ($).
- `actual_risk_usd`: Actual capital at risk after quantity rounding ($ \le \text{allowed\_risk\_usd}$).
- `reasons`: Comprehensive list of all check outcomes (`code`, `message`, `passed`).

---

## 3. Rules & Mathematical Formulas

### Rule 1: Per-Trade Risk
Allowed risk capital is calculated as a fraction of current equity:
$$\text{allowed\_risk} = \text{equity} \times R_{\text{trade}}$$
- Default: $R_{\text{trade}} = 1.0\%$ (`0.01`).
- Hard Cap: Config cannot exceed $2.0\%$ (`0.02`).
- Circuit Breaker Adjustment: If account drawdown $\ge 10\%$, $R_{\text{trade}}$ is automatically halved to $0.5\%$.

### Rule 2: Stop Distance Bounds
The distance between expected entry and stop loss:
$$d_{\text{stop}} = |\text{expected\_entry} - \text{stop\_loss}|$$
1. **Minimum Distance**: Reject if $d_{\text{stop}} < \max(0.5 \times \text{ATR}(\text{entry\_tf}), 0.3\% \times \text{entry})$. Prevents noise whipsaws.
2. **Maximum Distance**: Reject if $d_{\text{stop}} > 8.0\% \times \text{entry}$ (hard cap $15.0\%$). Prevents excessive capital lockup.
3. **Friction Limit**: Reject if estimated round-trip transaction costs (fees + slippage) $> 15\%$ of trade risk:
   $$\frac{\text{Estimated Friction}}{d_{\text{stop}}} > 0.15 \implies \text{REJECT}$$
   This principle halts trading when stops are too tight relative to spread and execution costs.

### Rule 3: Net Reward:Risk After Costs
Every trade must offer positive mathematical expectancy after trading friction:
$$\text{Net } R:R = \frac{\text{Gross Reward} - \text{Win Costs}}{\text{Gross Risk} + \text{Loss Costs}} \ge 1.5$$
- Where Win Costs include taker entry, slippage, and maker exit.
- Where Loss Costs include taker entry, slippage, and taker stop market exit + slippage.

### Rule 4: Position Sizing & Exchange Filters
$$\text{Raw Qty} = \frac{\text{allowed\_risk}}{d_{\text{stop}}}$$
$$\text{Qty} = \text{round\_down}(\text{Raw Qty}, \text{step\_size})$$
After rounding down:
$$\text{actual\_risk} = \text{Qty} \times d_{\text{stop}} \le \text{allowed\_risk}$$
Filter validation:
- $\text{Qty} \ge \text{min\_qty}$
- $\text{Qty} \le \text{max\_qty}$
- $\text{Qty} \times \text{entry} \ge \text{min\_notional}$

### Rule 5: Leverage Selection & Liquidation Buffer
Leverage is selected as the **lowest integer leverage** that fits the isolated margin within the account's available balance:
$$L = \max\left(1, \left\lceil \frac{\text{Notional}}{\text{available\_balance}} \right\rceil\right)$$
- Default Cap: $\le 5\times$ (Hard Cap: $\le 10\times$).
- **Liquidation Safety Buffer**: The liquidation price $P_{\text{liq}}$ must sit safely beyond the stop loss:
  $$\text{Buffer} = \max(1.0 \times \text{ATR}, 3.0\% \times \text{entry})$$
  - Long: $P_{\text{liq}} \le (\text{stop\_loss} - \text{Buffer})$
  - Short: $P_{\text{liq}} \ge (\text{stop\_loss} + \text{Buffer})$
  If the buffer is breached, the Risk Manager attempts lower leverage. If no viable leverage satisfies both margin and liquidation safety, the intent is rejected.

### Rule 6: Notional Cap
$$\text{Notional} \le 3.0 \times \text{equity} \quad (\text{Hard Cap } 5.0\times)$$

### Rule 7: Portfolio Limits (Phase 1)
- Max 1 open position total across all symbols. Any new entry intent is rejected while another position is active.

### Rule 8: Circuit Breakers (State Transitions)
Stateful risk limits monitored before every trade decision:
1. **Daily Realized Loss**: If $\sum \text{realized PnL today} \le -3.0\%$ of start-of-day equity, halt new entries until 00:00:00 UTC.
2. **Drawdown $\ge 10\%$**: Risk per trade is halved ($1.0\% \to 0.5\%$).
3. **Drawdown $\ge 15\%$**: Trading halted (paper-only mode in live).
4. **Drawdown $\ge 20\%$**: Full stop. Requires manual operator intervention with typed confirmation: `deskmind risk reset --confirm`.
5. **Loss Streak**: 4 consecutive losing trades triggers an automatic 24-hour cooling off period.

### Rule 9: Funding Rate Guard
If a funding event is scheduled within 30 minutes, and the funding rate is adverse to the position (Long paying positive rate, or Short paying negative rate) and exceeds $0.05\%$, the intent is rejected.

### Rule 10: Event Guard Hook
Interface for macro news / CPI / FOMC blackout windows. Currently returns passed (`allowed`), architected for external news feeds.

### Rule 11: Data Health Guard (Live Mode)
In live mode, if feed latency is stale or the preceding candle was flagged as degraded/missing, new entries are rejected.

---

## 4. Hard Safety Caps (Code Constants)

Config settings are strictly validated against immutable code constants:
```python
HARD_CAP_MAX_RISK_PCT = Decimal("0.02")        # Max 2% per trade
HARD_CAP_MAX_STOP_PRICE_PCT = Decimal("0.15")  # Max 15% stop distance
HARD_CAP_MAX_LEVERAGE = 10                     # Max 10x leverage
HARD_CAP_MAX_NOTIONAL_MULT = Decimal("5.0")    # Max 5x equity notional
```
Attempting to instantiate `RiskConfig` with values exceeding these limits raises an immediate `ValueError`.

---

## 5. CLI Verification Commands

### Check Trade Intent
```bash
uv run deskmind risk check --symbol BTCUSDT --side long --entry 60000 --stop 58000 --target 64000
```

### View Circuit Breaker Status
```bash
uv run deskmind risk status
```

### Reset 20% Drawdown Full Stop
```bash
uv run deskmind risk reset --confirm
```
