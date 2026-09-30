# Pre-registered Research Hypotheses (v1.0.0)

> [!IMPORTANT]
> All strategy parameters documented in this registry are **frozen as of pre-registration**.
> Any modification to entry, stop, target, timeframe, or filters constitutes a new hypothesis version (e.g. `H1_v1.0.1`) and increments the trial counter in the Research Registry.

---

## Hypothesis Registry (Frozen Parameters)

### H1: Discount OB / FVG Continuation
* **Id**: `H1_v1.0.0`
* **Version**: `1.0.0`
* **Timeframe**: 1h bias -> 15m zone -> 5m trigger
* **Logic**:
  * **Bullish**: 1h trend is `bullish` + price in 1h `discount` zone + fresh 15m bullish OB/FVG (`size_atr >= 0.5`) in zone + 5m `CHoCH_up`.
  * **Bearish**: 1h trend is `bearish` + price in 1h `premium` zone + fresh 15m bearish OB/FVG (`size_atr >= 0.5`) in zone + 5m `CHoCH_down`.
* **Entry**: Next 5m bar open ($T+1$).
* **Stop**: Beyond OB/FVG extreme low (bullish) or high (bearish) $+ 0.1 \times \text{ATR}_{1h}$.
* **Target**: Nearest 1h BSL/SSL or $2.0 R$ (whichever is closer, subject to minimum target $\ge 1.5 R$).
* **Rationale**: High-timeframe trend continuation from discounted institutional order blocks provides asymmetric risk-reward with strong structural backing.

---

### H2: Liquidity Sweep Reversal
* **Id**: `H2_v1.0.0`
* **Version**: `1.0.0`
* **Timeframe**: 15m pool sweep -> 5m trigger & entry
* **Logic**:
  * **Bullish**: Active session is `london_open` or `ny_am` killzone + strong 15m SSL pool swept + 5m `CHoCH_up` within $N=12$ bars.
  * **Bearish**: Active session is `london_open` or `ny_am` killzone + strong 15m BSL pool swept + 5m `CHoCH_down` within $N=12$ bars.
* **Entry**: Limit at 5m FVG Consequent Encroachment (CE: 50% midpoint of gap).
* **Stop**: Below sweep low (bullish) or above sweep high (bearish) $- 0.1 \times \text{ATR}_{1h}$.
* **Target**: Opposite strong liquidity pool (BSL for bullish entry, SSL for bearish entry).
* **Rationale**: Major session liquidity sweeps clear retail stops into institutional liquidity, creating rapid mean-reversion displacement.

---

### H3: Silver Bullet Window FVG
* **Id**: `H3_v1.0.0`
* **Version**: `1.0.0`
* **Timeframe**: 1h trend -> 5m Silver Bullet window
* **Logic**:
  * During active Silver Bullet window (`sb1` 3-4 AM NY, `sb2` 10-11 AM NY, or `sb3` 2-3 PM NY) + displacement-backed FVG forms in direction of 1h trend.
* **Entry**: Limit at FVG CE (50% midpoint).
* **Stop**: Beyond FVG candle 1 extreme (low for bullish, high for bearish).
* **Target**: Nearest liquidity pool or structural swing (minimum $\ge 1.5 R$).
* **Rationale**: Specific hourly algorithm delivery windows regularly create clean displacement gaps in line with higher-timeframe orderflow.

---

### H4: Support/Resistance Breakout-Retest
* **Id**: `H4_v1.0.0`
* **Version**: `1.0.0`
* **Timeframe**: 1h S/R zone -> 5m retest close
* **Logic**:
  * Price Action breakout of strength $\ge 2$ S/R zone in direction of 1h trend (`bo_min_atr >= 1.0`), followed by a successful retest that holds the zone boundary.
* **Entry**: Retest confirmation candle close.
* **Stop**: Beyond broken S/R zone edge $+ 0.2 \times \text{ATR}_{1h}$.
* **Target**: Fixed $2.0 R$.
* **Rationale**: Multi-touch horizontal support/resistance zones converted to opposite polarity offer reliable retest support during trending regimes.

---

### H5: PDH/PDL Sweep Reversal
* **Id**: `H5_v1.0.0`
* **Version**: `1.0.0`
* **Timeframe**: Daily key level -> 5m reversal trigger
* **Logic**:
  * In `london_open` or `ny_am` killzone + Previous Day High (PDH) or Previous Day Low (PDL) swept + 5m CHoCH against the sweep within 6 5m bars.
* **Entry**: Next 5m bar open ($T+1$).
* **Stop**: Beyond sweep extreme high/low.
* **Target**: NY Midnight Open price level or $2.0 R$ (whichever is closer, minimum $\ge 1.2 R$).
* **Rationale**: Daily extremes attract massive stop liquidity; when swept inside prime trading sessions, failed breakouts regularly revert to the daily opening anchor.
