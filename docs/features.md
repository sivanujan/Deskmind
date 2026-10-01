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
| `last_event_15m_confirmed_at` | Trend | `int64` | Bar close timestamp when 15m event confirmed (null if none) |
| `last_event_5m` | Trend | `string` | Most recent structure break on 5m (`BOS_up`, `BOS_down`, `CHoCH_up`, `CHoCH_down`, `none`) |
| `last_event_5m_confirmed_at` | Trend | `int64` | Bar close timestamp when 5m event confirmed (null if none, used for `setup_id`) |
| `pd_zone_1h` | ICT / SMC | `string` | Current 1h Premium/Discount zone (`premium`, `discount`, `equilibrium`, `none`) |
| `position_pct_1h` | ICT / SMC | `float64` | Price location in 1h range clamped to [0.0, 100.0] (0.0 = low, 100.0 = high, null if no range) |
| `outside_range_1h` | ICT / SMC | `bool` | True if price is outside the 1h dealing range (< 0% or > 100%) |
| `in_ote_1h` | ICT / SMC | `bool` | True if price is inside 1h Optimal Trade Entry zone (61.8% - 78.6% retracement) |
| `nearest_fvg_size_atr` | ICT / SMC | `float64` | Size of nearest active Fair Value Gap in 1h ATR units (0.0 if none) |
| `nearest_fvg_displacement` | ICT / SMC | `bool` | True if nearest active FVG was created by a displacement candle |
| `nearest_fvg_dist_atr` | ICT / SMC | `float64` | Distance from current price to nearest FVG boundary in 1h ATR units: `(boundary - close) / atr` (0.0 if inside) |
| `nearest_ob_state` | ICT / SMC | `string` | State of nearest active Order Block (`fresh`, `tested`, `none`) |
| `nearest_ob_dist_atr` | ICT / SMC | `float64` | Distance from current price to nearest OB boundary in 1h ATR units: `(boundary - close) / atr` (0.0 if inside) |
| `strong_pool_swept_side` | ICT / SMC | `string` | Side of most recent strong pool sweep (`BSL`, `SSL`, `none`). Expired after 48 5m bars (4h) |
| `strong_pool_swept_bars_ago` | ICT / SMC | `int32` | 5m bars elapsed since most recent strong pool sweep (-1 if none or expired > 48 bars) |
| `killzone` | ICT Time | `string` | Active killzone (`asian`, `london_open`, `ny_am`, `ny_lunch`, `london_close`, `none`) |
| `silver_bullet_active` | ICT Time | `bool` | True if currently in a Silver Bullet 1-hour window (3-4 AM, 10-11 AM, 2-3 PM NY) |
| `session` | ICT Time | `string` | Active main trading session (`asia`, `london`, `new_york`, `off_hours`) |
| `dist_to_pdh_atr` | ICT Time | `float64` | Distance to Previous Day High in 1h ATR units: `(pdh_price - close) / atr` (positive if below PDH) |
| `dist_to_pdl_atr` | ICT Time | `float64` | Distance to Previous Day Low in 1h ATR units: `(pdl_price - close) / atr` |
| `pdh_swept_recent` | ICT Time | `bool` | True if PDH was swept within the last 12 5m bars |
| `pdl_swept_recent` | ICT Time | `bool` | True if PDL was swept within the last 12 5m bars |
| `nearest_sr_strength` | Price Action | `int32` | Strength score of nearest S/R zone (0 to 100 scale from `SRZone.compute_strength()`) |
| `nearest_sr_touches` | Price Action | `int32` | Distinct touch candles count on the nearest active S/R zone (0 if none) |
| `nearest_sr_dist_atr` | Price Action | `float64` | Distance to nearest S/R zone boundary in 1h ATR units: `min(|close - top|, |close - bot|) / atr` (0.0 if inside) |
| `pattern_recent_type` | Price Action | `string` | Recent candle pattern (`hammer`, `shooting_star`, `engulfing_bull`, etc.). Expired after 12 bars (1h) |
| `pattern_recent_at_zone` | Price Action | `bool` | True if recent unexpired pattern occurred at an active S/R zone |
| `pattern_recent_with_trend` | Price Action | `string` | Alignment of unexpired pattern with 1h trend (`aligned`, `counter`, `none`) |
| `pattern_recent_bars_ago` | Price Action | `int32` | 5m bars elapsed since recent pattern confirmed (-1 if none or expired > 12 bars) |
| `weekend_flag` | System | `bool` | True if UTC bar close falls on Saturday or Sunday |
| `atr_pct` | System | `float64` | 1h ATR divided by current close price (volatility percentage) |
| `universe_member` | System | `bool` | True if symbol was an active universe constituent at timestamp $T$ |

---

## Known Limitations

1. **`nearest_sr_strength` Spread**: In the current implementation of `SRZone.compute_strength()`, approximately 99.3% of active zones fall into the narrow range 40–59 (2–3 touches with typical recency), providing almost no discriminatory power. A comprehensive redesign of the composite strength model is required in a future release. For structural zone filtering, use `nearest_sr_touches` instead.
2. **HTF Trend "Ranging" Transition**: `htf_trend_1h` only transitions to `"ranging"` after `max_break_idle_bars = 100` consecutive 1h bars without a structural break. During tight consolidation or whipsaws with frequent counter-swings under 100 bars, it maintains the prior directional bias and does not capture immediate market chop.

---

## Storage & Partitioning

Features are saved as **Apache Parquet** files partitioned by `symbol` and `month`:
`data/features/symbol={SYMBOL}/year_month={YYYY-MM}/features.parquet`

Compression format: `snappy` or `zstd`.
