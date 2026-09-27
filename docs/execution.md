# Deskmind Execution Architecture & Specification (Testnet & Paper)

## 1. Principles & Safety First

The Deskmind Execution engine translates approved risk decisions into exchange or simulated orders safely and deterministically.
It operates under strict safety invariants designed to prevent capital loss, race conditions, or unhedged exposure.

### Non-Negotiable Safety Constraints
1. **Practice/Testnet & Paper Only**: Mainnet hosts are blocked at configuration initialization and validated on every outgoing HTTP/WebSocket request. Any connection to mainnet is rejected immediately.
2. **Zero Secret Leakage**: API keys and secrets are parsed via `SecretStr` from `.env`. They are never logged, printed, serialized to JSON, or included in exception messages.
3. **Mandatory Protective Stop Invariant**: An open position without an active exchange-side stop for more than 10 seconds is an **EMERGENCY**. The system flattens the position immediately (reduce-only market order), logs `CRITICAL`, and latches a circuit breaker `HALT`.
4. **Deterministic Idempotency**: All orders generate a deterministic `newClientOrderId` derived from the trade `setup_id` and action. Before any retry, the exchange is queried by `clientOrderId` to prevent duplicate execution.
5. **Pre-Trade Testnet Divergence Validation**: Market data comes from mainnet; Binance testnet has its own independent orderbook. Before order placement, testnet prices are fetched and validated against Risk Manager bounds. If invalid, the intent is skipped with `TESTNET_PRICE_DIVERGENCE`.

---

## 2. Order Flow & Protective Invariants

```
                      [ Intent from Strategy ]
                                 │
                                 ▼
                     [ Risk Manager Evaluator ]
                                 │
                   Approved? ───►│◄─── Rejected? ──► [ Discard & Log ]
                                 │
                                 ▼
                       [ Approval Gate ]
                    (auto / manual CLI gate)
                                 │
                                 ▼
                     [ Pre-Trade Divergence ]
                    (mark & last price check)
                                 │
                                 ▼
                     [ Entry: Market Order ]
                    (deterministic clientOrderId)
                                 │
                                 ▼
                    [ Immediate Protective Orders ]
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
      [ STOP_MARKET (reduceOnly) ]   [ TAKE_PROFIT_MARKET (reduceOnly) ]
      • Trigger: CONTRACT_PRICE      • Trigger: CONTRACT_PRICE
      • Sized to actual fill qty     • Sized to actual fill qty
```

### Stop Trigger Price Type & Binance Algo Orders Specification
- **Type**: `CONTRACT_PRICE` (Binance Last Traded Price).
- **Rationale**: The backtest engine models bar execution and stop triggers using candle high/low prices (which reflect actual contract trade prints, not mark price index calculations). Using `CONTRACT_PRICE` aligns live execution strictly with the backtest simulation models.
- **Binance USDS-M Futures Algo Service Migration**:
  Binance migrated all conditional orders (`STOP`, `STOP_MARKET`, `TAKE_PROFIT`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET`) on USDS-M Futures to the dedicated **Algo Service**. Attempting to place conditional orders via the legacy `POST /fapi/v1/order` endpoint fails with:
  > `{"code": -4120, "msg": "Order type is not supported in this endpoint. Please use algo order API /fapi/v1/algoOrder"}`
  
  Deskmind uses the official Algo Service endpoints:
  - **Placing Conditional Orders**: `POST /fapi/v1/algoOrder`
    - `symbol`: e.g. `"BTCUSDT"`
    - `side`: `"BUY"` | `"SELL"`
    - `algoType`: `"CONDITIONAL"`
    - `type`: `"STOP_MARKET"` | `"TAKE_PROFIT_MARKET"`
    - `triggerPrice`: stop or take-profit price rounded to tick size
    - `quantity`: filled quantity rounded to step size
    - `reduceOnly`: `"true"`
    - `workingType`: `"CONTRACT_PRICE"`
    - `clientAlgoId`: deterministic client ID (e.g. `dm_s_<setup_id>`)
    - `positionSide`: `"BOTH"` (One-way mode)
  - **Querying Open Conditional Orders**: `GET /fapi/v1/openAlgoOrders`
  - **Cancelling Conditional Orders**: `DELETE /fapi/v1/algoOrder` (by `algoId` or `clientAlgoId`)
  - **Batch Cancelling**: `DELETE /fapi/v1/algoOpenOrders`
  
  The executor automatically queries and cancels across both regular and algo endpoints to guarantee no protective order is missed during reconciliation or kill switch activation.

### Partial Fills & Protective Resizing
When an entry order fills partially:
1. Protective stops and take-profits are immediately placed covering the **exact executed quantity**.
2. If subsequent fill chunks occur, protective orders are adjusted to match the total open position size.

---

## 3. Account Setup (Verified & Idempotent)

On executor startup for each configured symbol:
1. **Position Mode**: Verified and set to **One-Way Mode** via `POST /fapi/v1/positionSide/dual` (`dualSidePosition=false`). Handled gracefully if already configured.
2. **Margin Type**: Set to **ISOLATED** via `POST /fapi/v1/marginType`.
3. **Leverage**: Set per symbol to the exact leverage specified by the Risk Manager decision via `POST /fapi/v1/leverage`.
4. **Exchange Filters**: Refreshed periodically from `/fapi/v1/exchangeInfo` (step size, tick size, min/max quantity, min notional).

---

## 4. Reconciliation Policy (Startup + Every 60s)

The reconciliation engine continuously compares internal database state against exchange positions and open orders. It automatically resolves 4 primary mismatch scenarios:

| Scenario | Detection Condition | Action Taken |
| :--- | :--- | :--- |
| **Position Without Stop** | Exchange position has `qty > 0` but no active `STOP_MARKET` order | Immediately attempt to place protective stop. If not resolved within 10s watchdog, flatten position via market reduce-only and halt. |
| **Orphan Orders** | Open stop/TP order on exchange, but no corresponding open position | Immediately cancel orphan order (`DELETE /fapi/v1/order`). |
| **DB Open / Exchange Closed** | Database records an open trade, but exchange position is 0 (e.g. stop hit while offline) | Query recent user trades (`/fapi/v1/userTrades`), calculate realized PnL and exit price, record trade closure in `trade_journal`. |
| **Unknown Position** | Exchange position exists but no record in DB or active strategy | Log `CRITICAL` alert, flatten position immediately via reduce-only market order, log reconciliation fix. |

### Mid-Position Restart Recovery
If the trading process crashes or restarts while a position is open:
1. On boot, `reconcile_all()` runs before processing new signals.
2. It discovers the active exchange position, links existing exchange-side stop orders, and synchronizes the journal state.
3. No duplicate orders are placed because deterministic client order IDs prevent re-submission.

---

## 5. Kill Switch Architecture

The Kill Switch acts as an absolute emergency circuit breaker:
- **CLI Trigger**: `uv run deskmind trade kill --confirm`
- **Automatic Triggers**:
  1. Circuit breaker transitions to `full_stop`.
  2. Protective stop placement fails repeatedly after retries.
  3. Emergency unstopped position timer exceeds 10 seconds.
  4. System clock drift against exchange server time exceeds 1.0 second.
  5. Repeated unhandled REST API 5xx errors.
  6. Unresolvable reconciliation mismatch.

### Kill Procedure
1. Cancel all open orders across all configured symbols in parallel.
2. Query all open positions.
3. Flatten all open positions immediately using reduce-only market orders.
4. Latch executor halt state (`HALT`) preventing any further orders until manual intervention.

---

## 6. Fault Injection Framework (`faults.py`)

For automated testing and server stress verification, the engine includes a test-only fault injector (`FaultInjector`). It is disabled by default and strictly refuses execution in live mode.

Available Fault Hooks:
- `ws_disconnect`: Force-terminates the WebSocket user data stream to verify automatic reconnection and message recovery.
- `stop_keepalive`: Suspends user data stream listenKey keepalive pings to test expired listenKey handling and recovery.
- `stop_failure`: Simulates an exchange error when placing protective stops to test the 10s emergency flatten watchdog and halt latch.
- `api_5xx`: Injects transient 500/502/503 HTTP server errors into REST client calls to verify exponential backoff and retry logic.

---

## 7. Approval Gate & Workflow

Operating modes (`APPROVAL_MODE`):
- `auto`: Automatically passes approved Risk Manager decisions directly to the executor (testnet/paper only).
- `manual`: Trade intents enter a `pending` state awaiting human operator confirmation.
  - Pending requests expire automatically after 10 minutes (`PENDING_EXPIRY_SECONDS = 600`).
  - Upon CLI approval (`uv run deskmind trade approve <id>`), the intent is **re-checked** against the Risk Manager using the latest account balance and market price before execution.
  - CLI rejection (`uv run deskmind trade reject <id>`) marks the intent as rejected.

CLI Commands:
```bash
# View pending trade requests
uv run deskmind trade pending

# Approve pending trade by ID
uv run deskmind trade approve <req_id>

# Reject pending trade by ID
uv run deskmind trade reject <req_id>
```

---

## 8. Paper Executor (`paper.py`) & Shared Fill Model (`fills.py`)

The Paper Executor implements the exact same `BaseExecutor` ABC interface as `BinanceTestnetExecutor`.
- Tracks simulated account balance, margins, positions, and open orders in memory.
- Uses the **exact same unified fill model** (`src/deskmind/backtest/fills.py`) as the backtest engine:
  - `calc_slippage_pct(atr, price)`: Dynamic slippage combining 2 bps base + 0.05 ATR fraction.
  - `simulate_market_fill(target_price, side, qty, atr, is_maker)`: Fills market orders with dynamic adverse slippage and computes fees (0.02% maker, 0.05% taker).
  - `simulate_candle_exit(candle, side, stop_loss, take_profit, atr)`: Evaluates intra-candle stop and take-profit triggers with proper priority (worst-case stop fill when both trigger in one bar).
- Fixed Paper Executor Bug: TP and SL triggers in `check_triggers_on_candle` pass explicit trigger prices to `close()`, ensuring take-profit exits fill at trigger price (e.g. $64,000) rather than falling back to current candle close ($60,000).
- Backtest invariance verified: Running `trend_follow_v1` on TRAIN after migrating to the shared fill model yields 100% identical results (`$-1,507.21` return, 119 trades, 30.3% win rate).

---

## 9. Trade Journal & Execution Overlays (PostgreSQL)

All trade lifecycle events (intents, order executions, position fills, and exits) are recorded directly into PostgreSQL:
- **Database Table**: `trade_journal` (Alembic migration `0004_trade_journal_execution_fields.py`)
  - Columns: `id`, `created_at`, `symbol`, `side`, `entry`, `sl`, `tp`, `size`, `leverage`, `status`, `pnl`, `exit_price`, `exit_time`, `exit_reason`, `fees_paid`, `trading_mode`, `orders`, `risk_decision`.
- **API Overlays**: Served directly through the read-only `/api/overlays` endpoint via `load_execution_overlays`.
  - Maps entries to `trade_entry` markers (green/red arrows) and exits to `trade_exit` markers (sky-blue circles).
  - Source agent labeled as `exec:testnet` or `exec:paper`.
  - File-based JSONL is retained strictly as an offline emergency fallback / local debug export.
