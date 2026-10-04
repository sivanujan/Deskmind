# Research Methodology Changes & Log

## 2026-10-04 — Introduction of `per_symbol_independent` Research Mode

### 1. Context & Empirical Finding
During the initial multi-symbol evaluation of **H6_v1.0.0** (4h Trend Breakout), a severe discrepancy was observed between single-symbol and multi-symbol execution:
- **BTCUSDT Single-Symbol Timing Test**: Generated **118 executed trades**.
- **10-Symbol Portfolio TRAIN Run**: BTCUSDT produced only **4 executed trades** (a 96.6% drop), with total trades across all 10 symbols reaching only **153 trades**.
- **Audit of Signal Emissions**: Analyzing the raw execution logs revealed that **2,119 valid signals** were emitted by H6 across the 10 symbols over the 2-year TRAIN period, but only **153 became trades** (a **7.2% conversion rate**).

```
Symbol       | Signals Emitted | Trades Executed | Conversion %
--------------------------------------------------------------
ADAUSDT      | 229             | 8               | 3.5%
BNBUSDT      | 226             | 17              | 7.5%
BTCUSDT      | 195             | 4               | 2.1%
DOGEUSDT     | 201             | 8               | 4.0%
ETHUSDT      | 218             | 11              | 5.0%
LINKUSDT     | 229             | 11              | 4.8%
LTCUSDT      | 208             | 13              | 6.2%
MATICUSDT    | 195             | 27              | 13.8%
SOLUSDT      | 241             | 25              | 10.4%
XRPUSDT      | 177             | 29              | 16.4%
--------------------------------------------------------------
Total        | 2,119           | 153             | 7.2%
```

### 2. Root Cause Analysis
In the standard backtest engine, portfolio allocation enforces `max_open_positions = 1` globally across all symbols. When a strategy takes multi-day swing positions (such as H6's 10-day maximum hold or trailing trend exit), a single position in one symbol (e.g. `XRPUSDT`) blocks **all other 9 symbols** from entering any trades for days or weeks at a time.

This introduces a fatal flaw in hypothesis evaluation:
1. **Conflating Alpha with Portfolio Allocation**: Research must measure pure **signal quality and statistical edge** ($\mathbb{E}[R]$, win rate, bootstrap percentile vs. random walk), **not** artificial capital capacity constraints or cross-symbol competition.
2. **Path-Dependent Starvation**: Which signal gets taken becomes a random artifact of which symbol happened to break out a few hours earlier, discarding valid signals and destroying sample size.

### 3. Method Fix: `per_symbol_independent` Mode
Effective 2026-10-04, all hypothesis research evaluations operate in `per_symbol_independent` mode:
- **Independent Execution**: Each universe symbol runs its own isolated backtest engine instance.
- **Position Constraint**: Exactly **max 1 open position per symbol** (intra-symbol position stacking is prevented, but zero cross-symbol blocking occurs).
- **Random Baseline**: The 1000-seed matched random baseline operates in the same independent mode, matching each trade strictly against that symbol's historical bar stream without cross-symbol interference.
- **Gate Evaluation**: Promotion gates are strictly evaluated on **$R$-multiple statistics**:
  - Sample size $n = \sum_s n_s$
  - Pooled gross expectancy $\mathbb{E}[R_{\text{gross}}] \ge 0.15R$
  - Pooled net expectancy $\mathbb{E}[R_{\text{net}}] > 0.0R$
  - Matched random baseline percentile $\ge 95.0\%$
  - Consistency across universe (positive symbols $\ge 60\%$)
  - Temporal stability (both 2023 and 2024 net $R > 0$)
  - Deflated Sharpe Ratio $DSR(K=10)$
- **Dollar PnL and Drawdown Reporting**: Because independent runs assume dedicated capital per symbol rather than a shared cash pool, total dollar PnL ($\$$) and Maximum Drawdown ($\%$) are reported on a **per-symbol basis**.
