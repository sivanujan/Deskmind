# Feature Store Specification (Point-in-Time)

The Deskmind Feature Store builds a flat 5-minute feature row per symbol at every 5m bar close from all four agents' point-in-time states as of that time $T$.

All feature calculations use strictly historical or current $T$ closed bar data (`reconstruct_state(T)` or live engine state at $T$). No look-ahead is permitted.

---

## Feature Schema

| Feature Name | Source Agent | Type | Description |
|---|---|---|---|
| `timestamp` | System | `int64` | 5m bar close time in UTC epoch milliseconds ($T$) |
| `symbol` | System | `string` | Symbol identifier (e.g., `BTCUSDT`) |
| `htf_trend_2h` | Trend | `string` | Trend direction on 2h timeframe (`bullish` / `bearish` / `ranging`) |
| `htf_trend_1h` | Trend | `string` | Trend direction on 1h timeframe (`bullish` / `bearish` / `ranging`) |
| `last_event_15m` | Trend | `string` | Most recent structure break on 15m (`BOS_up`, `BOS_down`, `CHoCH_up`, `CHoCH_down`, `none`) |
| `last_event_5m` | Trend | `string` | Most recent structure break on 5m (`BOS_up`, `BOS_down`, `CHoCH_up`, `CHoCH_down`, `none`) |
| `pd_zone_1h` | ICT / SMC | `string` | Current 1h Premium/Discount zone (`premium`, `discount`, `equilibrium`, `none`) |
| `position_pct_1h` | ICT / SMC | `float64` | Price location in 1h range (0.0 = low, 1.0 = high, null if no range) |
| `in_ote_1h` | ICT / SMC | `bool` | True if price is inside 1h Optimal Trade Entry zone (61.8% - 78.6% retracement) |
| `nearest_fvg_size_atr` | ICT / SMC | `float64` | Size of nearest active Fair Value Gap in 1h ATR units (0.0 if none) |
| `nearest_fvg_displacement` | ICT / SMC | `bool` | True if nearest active FVG was created by a displacement candle |
| `nearest_fvg_dist_atr` | ICT / SMC | `float64` | Distance from current price to nearest FVG edge in 1h ATR units |
| `nearest_ob_state` | ICT / SMC | `string` | State of nearest active Order Block (`fresh`, `mitigated`, `none`) |
| `nearest_ob_dist_atr` | ICT / SMC | `float64` | Distance from current price to nearest OB boundary in 1h ATR units |
| `strong_pool_swept_side` | ICT / SMC | `string` | Side of most recent strong pool sweep (`BSL`, `SSL`, `none`) |
| `strong_pool_swept_bars_ago` | ICT / SMC | `int32` | 5m bars elapsed since most recent strong pool sweep (-1 if none) |
| `killzone` | ICT Time | `string` | Active killzone (`asian`, `london_open`, `ny_am`, `ny_lunch`, `london_close`, `none`) |
| `silver_bullet_active` | ICT Time | `bool` | True if currently in a Silver Bullet 1-hour window (3-4 AM, 10-11 AM, 2-3 PM NY) |
| `session` | ICT Time | `string` | Active main trading session (`asia`, `london`, `new_york`, `off_hours`) |
| `dist_to_pdh_atr` | ICT Time | `float64` | Distance to Previous Day High in ATR units (negative if below, positive if above) |
| `dist_to_pdl_atr` | ICT Time | `float64` | Distance to Previous Day Low in ATR units |
| `pdh_swept_recent` | ICT Time | `bool` | True if PDH was swept within the last 12 5m bars |
| `pdl_swept_recent` | ICT Time | `bool` | True if PDL was swept within the last 12 5m bars |
| `nearest_sr_strength` | Price Action | `int32` | Touch count strength of nearest S/R zone (0 if none) |
| `nearest_sr_dist_atr` | Price Action | `float64` | Distance to nearest S/R zone boundary in 1h ATR units |
| `pattern_recent_type` | Price Action | `string` | Recent candle pattern type (`hammer`, `shooting_star`, `engulfing_bull`, `engulfing_bear`, `doji`, `none`) |
| `pattern_recent_at_zone` | Price Action | `bool` | True if recent pattern occurred at an active S/R zone |
| `pattern_recent_with_trend` | Price Action | `string` | Alignment of pattern with 1h trend (`aligned`, `counter`, `none`) |
| `weekend_flag` | System | `bool` | True if UTC bar close falls on Saturday or Sunday |
| `atr_pct` | System | `float64` | 1h ATR divided by current close price (volatility percentage) |
| `universe_member` | System | `bool` | True if symbol was an active universe constituent at timestamp $T$ |

---

## Storage & Partitioning

Features are saved as **Apache Parquet** files partitioned by `symbol` and `month`:
`data/features/symbol={SYMBOL}/year_month={YYYY-MM}/features.parquet`

Compression format: `snappy` or `zstd`.
