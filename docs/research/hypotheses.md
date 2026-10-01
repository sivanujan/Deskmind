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

### H4: Support/Resistance Breakout-Retest (v1.0.0 - VOID)
* **Id**: `H4_v1.0.0`
* **Status**: **VOID (Spec/Scale Mismatch)**
* **Audit Note**: Originally specified `strength >= 2` assuming a touch counter, but `nearest_sr_strength` in Price Action engine is a composite 0–100 score (`SRZone.compute_strength()`) where 99.3% of zones score 40–59. This made the filter trivially true for all zones. Counted as Trial #1 for H4 in Research Registry.
* **Logic**: Breakout of strength $\ge 2$ S/R zone in direction of 1h trend followed by retest holding zone boundary.

---

### H4: Support/Resistance Breakout-Retest (v1.1.0 - Pre-registered)
* **Id**: `H4_v1.1.0`
* **Version**: `1.1.0`
* **Status**: **ACTIVE / PRE-REGISTERED** (Trial #2 for H4)
* **Timeframe**: 1h S/R zone -> 5m retest close
* **Logic**:
  * Price Action breakout of a confirmed horizontal S/R zone with **`nearest_sr_touches >= 3`** (at least 3 distinct touch candles on the 1h timeframe).
  * Breakout occurs in the direction of the 1h trend.
  * Price retests the broken S/R zone boundary (`nearest_sr_dist_atr <= 0.5`) and prints an aligned candlestick pattern (`pattern_recent_at_zone == True` and `pattern_recent_with_trend == "aligned"` within 12 bars).
* **Entry**: Retest confirmation candle close.
* **Stop**: Beyond broken S/R zone edge $+ 0.2 \times \text{ATR}_{1h}$.
* **Target**: Fixed $2.0 R$.
* **Rationale**: S/R zones with 3+ distinct touches demonstrate proven institutional memory; retests of these boundaries during trending conditions offer asymmetric continuation entries.

---

## Known Research Limitations

1. **`nearest_sr_strength` Scale & Spread**: The composite strength metric `nearest_sr_strength` clusters tightly between 40 and 59 (99.3% of bars) and lacks discriminative power. It is scheduled for an architectural overhaul in Phase 11. Hypotheses must use structural discrete touches (`nearest_sr_touches`) rather than composite strength.
2. **HTF Trend "Ranging" Transition**: `htf_trend_1h` only transitions to `"ranging"` after `max_break_idle_bars = 100` consecutive 1h bars without a structural swing break. During tight consolidation or chop under 100 bars, it retains the directional bias of the previous swing break.

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
