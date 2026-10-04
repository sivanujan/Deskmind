# TAS Phase 2 — Multi-Coin Universe Scan (4h)

**Date**: 2026-10-04  
**Status**: Pre-registered (Locked prior to execution)  
**Strategy**: TAS (Trend Analysis Strategy)  
**Timeframe**: 4h  

---

## 1. Objective

Scale the Trend Analysis Strategy (TAS) across the full Binance USDT-M perpetual futures universe on the **4h timeframe** to test whether trend-following alpha persists across multiple crypto assets under realistic liquidity constraints and tiered execution friction.

---

## 2. Strategy Configuration

Strategy parameters use the exact Pine Script v6 TAS defaults (identical to Phase 1):
- **Trend Tracker**: WMA(true range, 14) $\times$ 2.2 ratchet
- **Equilibrium**: HMA(LinReg(close, 20), 20)
- **RSI**: Length 14, smoothed SMA 14
- **DMI / ADX**: Length 14
- **Entry Filter**: Trend score $\ge 50\%$ (at least 3 of 6 components), delayed entry allowed within 10 bars of flip
- **Exits**:
  - Target 1: 1.5R (33% size)
  - Target 2: 2.5R (33% size)
  - Target 3: 3.5R (remainder)
  - Stop to Breakeven after TP1 hit
  - Protective initial stop at Trend Tracker level
  - Market exit on opposite trend flip
- **Account & Sizing**:
  - Initial capital: $10,000
  - Sizing: 10% of current equity per position
  - Commission: 0.05% (5 bps) per fill (taker/maker unified conservative rate)
  - Execution: Signals evaluated on bar close; market entries/exits execute at next bar open; stops/limits execute intrabar with TradingView path emulation

---

## 3. Universe & Liquidity Filtering

- **Base Universe**: All USDT-M perpetual contracts from `reports/research/universe_2022_2025.json` (620 symbols, including active and delisted).
- **Exclusions**: Non-crypto perps (TSLA, AMZN, GOOGL, XAU, XAG, SPX, BTCDOM) are excluded from the research universe.
- **Dynamic Liquidity Eligibility**:
  - Evaluated at each signal bar using the 30-day rolling median daily quote volume from `data/daily_quote_volume.parquet` (using preceding closed days with zero lookahead).
  - A coin is **eligible for entry** only if its 30-day median quote volume $\ge \$20\text{M}$ at the signal bar.
  - If 30-day median quote volume $< \$20\text{M}$, no new entry signal is permitted.

---

## 4. Liquidity-Tiered Slippage Model

To model execution reality across market cap tiers, adverse slippage applied to market orders (entry, stop loss, and flip exits) is tiered by the 30-day median quote volume ($V$) at the time of execution:

| 30-Day Median Quote Volume ($V$) | Liquidity Tier | Slippage Applied (Market / Stop Fills) |
| :--- | :--- | :--- |
| $V \ge \$500\text{M}$ | Mega / High Cap | **1 tick** |
| $\$100\text{M} \le V < \$500\text{M}$ | Mid Cap | **0.02%** (2 bps), minimum 1 tick |
| $\$20\text{M} \le V < \$100\text{M}$ | Small / Illiquid Cap | **0.05%** (5 bps), minimum 1 tick |

Limit orders (TP1, TP2, TP3) execute on touch without slippage.

---

## 5. Data Splits & Protocols

- **TRAIN Window**: `2023-01-01` to `2024-12-31` (24 months)
  - Indicators warm up on all available native history prior to `2023-01-01`.
  - Used for coin discovery and selection.
- **VALIDATION Window**: `2025-01-01` to `2025-12-31` (12 months)
  - Indicators warm up on all available native history prior to `2025-01-01`.
  - Executed on the frozen list of SELECTED coins.
- **TEST Window**: `2026-01-01` onwards
  - **STRICTLY LOCKED**. No backtest may access or evaluate 2026 data.

---

## 6. Selection Gates (Evaluated on TRAIN 2023–2024)

A coin is admitted to the **SELECTED list** if and only if it meets all of the following four criteria in TRAIN:
1. **Trade Count**: Number of positions $\ge 30$
2. **Profit Factor**: $\text{Gross Profit} / \text{Gross Loss} \ge 1.30$
3. **Expectancy**: Mean $R > 0$
4. **Drawdown**: Max Drawdown $\le 20.0\%$

The SELECTED coin list is frozen and committed to git before VALIDATION begins.

---

## 7. Validation Pass / Fail Criteria (Evaluated on VALIDATION 2025)

The hypothesis that TAS provides robust out-of-sample trend alpha passes if and only if **all three** conditions hold on the SELECTED coins in VALIDATION (2025-01-01 → 2025-12-31):
1. **Pooled Expectancy**: Pooled Mean $R > 0$
2. **Pooled Profit Factor**: Pooled $\text{PF} \ge 1.20$
3. **Cohort Breadth**: $\ge 60\%$ of selected coins achieve positive net profit in 2025.

### Comparison Benchmark
In addition, the 2025 pooled performance across **ALL eligible coins** will be computed to measure whether the selection filter added true selection alpha versus the broad trend beta of the crypto universe.
