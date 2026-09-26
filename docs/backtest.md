# Deskmind Backtest Engine — Specification & Assumptions

## 1. Overview & Core Philosophy
The Deskmind Backtest Engine provides realistic, event-driven simulation for Binance USDT-M perpetual futures.

Its sole purpose is **to tell the truth, not to make results look good**. It makes look-ahead cheating structurally impossible by design:
- Reuses the **exact same incremental agent code** (`SymbolTrendAgent`, `TrendEngine`) as the live production service.
- Operates on a strict **5m base clock**.
- Higher timeframe bars (15m, 1h, 2h) become visible to strategies only after their respective close timestamp has completed.
- Strategies never touch raw fills, fees, or account balances directly.

---

## 2. Execution Realism

### 2.1 Next-Bar Fills for Market Orders
- **Market entries** fill at the **NEXT bar's open price + slippage**.
- Orders are **never filled at the signal bar's close**. When a signal fires at 10:00 (close of the 09:55–10:00 candle), the order is placed for execution at the 10:00 open of the next bar.

### 2.2 Limit Orders: Trade-Through Requirement
- Limit orders fill **only if price trades strictly THROUGH the limit price**:
  - Buy limit at $P$: Requires bar low $< P$ (not merely touching $P$).
  - Sell limit at $P$: Requires bar high $> P$.
- An unfilled limit order remains on the book until filled or cancelled.
- Fills receive the **maker fee rate** (0.02% VIP0 default).

### 2.3 Stop-Loss & Gap Fills
- Stop-loss orders are modeled as exchange-side stop-market orders:
  - Normal trigger: Fills at the stop price $\pm$ slippage.
  - **Gap-through fill**: If a bar opens beyond the stop price (e.g. an abrupt market drop below stop), the order fills at the **bar open price**, reflecting the actual worse fill received in live conditions.
- Stop-market fills pay the **taker fee rate** (0.05% VIP0 default).

### 2.4 Same-Bar SL and TP Conservative Rule
- If both the Stop-Loss and Take-Profit price levels fall within the high/low range of the same 5m bar:
  - The engine **conservatively assumes the Stop-Loss was hit first**.
  - These occurrences are recorded and reported in `same_bar_sl_tp_count`.

### 2.5 Fee Structure
- Binance USDT-M VIP0 standard rates:
  - **Maker Fee Rate**: `0.0002` (0.02% / 2 bps).
  - **Taker Fee Rate**: `0.0005` (0.05% / 5 bps).
- Fees are deducted in USDT from the cash balance upon every trade entry and exit.

### 2.6 Slippage Model
- Slippage is calculated dynamically per fill:
  $$\text{Slippage} = \text{Fixed BPS} + \text{ATR Factor} \times \left(\frac{\text{ATR}}{\text{Price}}\right)$$
- Defaults:
  - Fixed BPS: `2.0` bps (`0.0002`).
  - ATR Factor: `0.05` (5% of 15m/1h ATR relative to price).
- Slippage increases transaction costs realistically during volatile market regimes.

### 2.7 Historical Funding
- Perpetual contracts incur 8-hourly funding payments applied at actual historical funding timestamps (00:00, 08:00, 16:00 UTC):
  $$\text{Funding Payment} = \text{Position Notional} \times \text{Funding Rate}$$
- Longs pay positive funding and receive negative funding.
- Shorts receive positive funding and pay negative funding.
- Funding payments are deducted or credited to the account cash balance on open positions.

### 2.8 Liquidation Against Mark Price
- Liquidation is evaluated against **5m MARK price klines** (mark high/low), never the last traded price.
- Maintenance margin rates (MMR) are calculated using Binance tiered maintenance margin brackets.
- A liquidation event terminates the position with the full margin loss and deducts the exchange liquidation clearance fee (default 1.25%).

### 2.9 Exchange Filters & Rounding
- All money, price, and quantity arithmetic is conducted using `Decimal`.
- Filter parameters are loaded from official Binance `exchangeInfo` snapshots (`data/meta/{symbol}_filters_*.json`):
  - Prices are rounded to `tickSize` using `ROUND_HALF_UP`.
  - Quantities are rounded down to `stepSize` using `ROUND_DOWN` (preventing margin overruns).
  - Orders violating `minQty`, `maxQty`, or `minNotional` are rejected and logged.

### 2.10 Exchange Data Gaps
- Known exchange outages (such as the ~25 minute Binance outage on 2023-11-10) are detected via timestamp discontinuity ($> 300,000$ ms).
- No orders fill inside a data gap.
- Positions that span across a data gap are flagged with `spanned_gap = True`.

### 2.11 Position Rule (Phase 1)
- Maximum **ONE open position** across all symbols at any time.
- If multiple symbols produce valid entry signals on the same bar close, signals are ranked deterministically:
  - Primary sort: `ranking_score` (strategy-defined, e.g. 1h ATR-normalized distance).
  - Tie-breaker: Alphabetical symbol order.
- The highest-ranked signal is selected for pending execution; all other signals are logged as skipped.

### 2.12 Warm-up Period
- A configurable warm-up period (default: **30 days**, configurable via `--warmup-days`) during which incremental agents are fully fed historical candles across all timeframes to stabilize indicator state (EMAs, ATRs, swing pivots).
- No trading signals are generated or executed during this period (`t_close < trading_start_ms`), ensuring strategies never trade on cold or uninitialized agent state.

### 2.13 Execution Invariants & Safety Enforcement
To guarantee safety and simulation integrity, the engine strictly enforces four loud invariants:
1. **Invariant (a) — Mandatory Active Stop**: Every open position and entry intent must maintain an active, positive stop-loss at all times. Any order without a valid stop fails the run immediately with a `RuntimeError`.
2. **Invariant (b) — Stop Strictly Inside Liquidation**: For every position, the stop-loss must be strictly inside the liquidation price with a safety buffer (default: 0.5%):
   - Longs: $\text{Stop} \ge \text{Liquidation Price} \times (1 + \text{buffer})$
   - Shorts: $\text{Stop} \le \text{Liquidation Price} \times (1 - \text{buffer})$
3. **Invariant (c) — True Liquidation Loss Accounting**: A liquidation exit records the exact loss equal to the full isolated margin plus exchange liquidation clearance fees. The R-multiple is calculated based on the actual loss relative to initial risk.
4. **Invariant (d) — Dedicated Own-Symbol Evaluation**: Stop-loss and liquidation triggers are evaluated on every single 5m bar of the position's own symbol, preventing any cross-symbol price bleeding. Furthermore, price-movement precedence guarantees that if a bar crosses both the stop and liquidation price, the stop-loss order is filled first.

---

## 3. Data Splits & Locked Test Split

To prevent look-ahead bias and multiple-testing p-hacking, historical data is partitioned into three splits:

| Split | Start Date | End Date | Purpose | Access Control |
|---|---|---|---|---|
| **train** | 2023-01-01 | 2024-12-31 | Model development, strategy logic, hypothesis testing | Open |
| **validation** | 2025-01-01 | 2025-12-31 | Parameter verification, out-of-sample validation | Open |
| **test** | 2026-01-01 | Latest | Final benchmark evaluation | **LOCKED** |

### Test Split Lock Policy
- Any backtest targeting the `test` split or extending into the test date range is **strictly refused by default**.
- Running on the test split requires:
  1. Passing `--unlock-test`.
  2. Passing `--confirm-test-unlock CONFIRM`.
- Every test-split run is permanently recorded in the audit registry.

---

## 4. Multiple-Testing Run Registry

Every backtest execution is recorded immutably:
- PostgreSQL database table: `backtest_runs`.
- Local audit log: `reports/backtests/registry.json`.

Recorded fields:
- `run_id`: Unique identifier (e.g. `bt_20260926_212630_123456`).
- `created_at`: Run timestamp (UTC).
- `strategy_name` & `strategy_version`.
- `params`: Strategy configuration (JSON).
- `agent_versions`: Trend agent version map.
- `symbols`: Evaluated trading pairs.
- `split` & `start_date` / `end_date`.
- `engine_version` & `git_commit`.
- `metrics`: Comprehensive performance statistics (JSON).
- `notes`: User notes.

`deskmind backtest runs list` displays all recorded runs to ensure full accountability across iterations.

---

## 5. Performance Metrics & Comparative Baselines

### 5.1 Key Metrics
- **Net Return ($ and %)** and **CAGR**.
- **Max Drawdown ($ and %)** and **Max Drawdown Duration**.
- **Sharpe Ratio** (annualized, 0% risk-free rate) and **Sortino Ratio**.
- **Win Rate (%)**, **Profit Factor**, and **Average R**.
- **Expectancy (% and R-multiple)**.
- **Gross Expectancy (R-multiple before fees, slippage, and funding)**.
- **Average Cost per Trade (R-multiple friction drag)**.
- **Exposure Time (%)** and **Average Holding Period**.
- **Total Fees**, **Total Funding Paid**, and **Liquidations Count**.
- **Same-bar SL+TP count** and **Longest Losing Streak**.

### 5.2 Comparative Baselines
1. **Buy & Hold Baseline**:
   - Equal-weighted holding across all symbols over the exact date range.
   - Realistically accounts for entry/exit taker fees and cumulative historical funding payments.
2. **Random-Entry Baseline**:
   - Matches the strategy's exact trade count, symbol distribution, and long/short direction mix.
   - Retains the strategy's stop/target holding rules with randomized entry timestamps across 50 random seeds.
   - Strategy performance is reported as an empirical percentile ($0\text{--}100\%$).
   - If the strategy fails to beat the majority of random seeds, this fact is stated clearly.

---

## 6. Chart & Replay Integration
Backtest runs generate visual overlay markers for the chart viewer (`apps/web`):
- Source agent tag: `backtest:{run_id}`.
- Entry markers (green for win, red for loss).
- Exit markers and SL/TP target levels.
- Full compatibility with historical candle playback and Replay mode.
