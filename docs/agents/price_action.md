# Price Action agent  rule specification

Agent name `price_action`, version **1.0.0** (`src/deskmind/agents/price_action/config.py`).
Deterministic Python, no LLM. Timeframes: 5m, 15m, 1h, 2h (per symbol).

---

## 0. No-look-ahead rule

Every item carries `created_time` and `confirmed_at` (epoch ms UTC). As-of queries
use only items with `confirmed_at <= T`. One code path for live and batch.

---

## 1. Support / Resistance zones

Cluster confirmed swing highs/lows (from the Trend agent) within ATR-based tolerance.
- **Zone**: a price band `[bottom, top]` formed when = 2 swings are within
  `sr_cluster_atr` × ATR of each other. The zone midpoint is the average of clustered
  swing prices; width = `sr_zone_width_atr` × ATR minimum.
- **Strength score** (0100) is the sum of weighted criteria:
  - touches (distinct candles that entered the zone): +10 per touch (max 40)
  - rejections (wick into zone + close outside): +15 per rejection (max 45)
  - age penalty: -1 per 100 bars since last touch (min 0)
  - timeframe multiplier: 5m=1.0, 15m=1.2, 1h=1.5, 2h=2.0
  - volume at touches vs ATR-period median: +5 if above median

| Parameter           | Default | Meaning                                    |
|---------------------|---------|--------------------------------------------|
| `sr_cluster_atr`    | 0.3     | Swing proximity to cluster (x ATR)         |
| `sr_zone_width_atr` | 0.15    | Minimum zone half-width (x ATR)            |
| `sr_min_touches`    | 2       | Minimum touches to form a zone             |
| `sr_max_age_bars`   | 500     | Retire zones with no interaction after N bars|

States: `active` | `broken` | `flipped` | `retired`.

Stable ids: `sr_{side}_{pivot_times_joined}` where `side` = `support` or `resistance`,
`pivot_times_joined` = `_`-separated confirmed_at ms of the swings that formed it.

---

## 2. Role reversal

- **Support ? Resistance**: price closes through a support zone (state ? `broken`);
  price later pulls back into the zone from below. State becomes `flipped`.
- **Resistance ? Support**: mirror.
- Transition recorded as a state transition with a new `flipped_at` timestamp.

---

## 3. Breakout + retest

A breakout is confirmed when a candle **closes** beyond a zone by = `bo_min_atr` × ATR.
A retest is a subsequent candle that re-enters the zone and **closes back in the
breakout direction** within `bo_retest_max_bars` bars.

Record: `breakout_time`, `retest_time`, `outcome` (`held` | `failed`).
`held` = retest close confirms breakout direction. `failed` = retest close reverses.

| Parameter          | Default | Meaning                                         |
|--------------------|---------|------------------------------------------------|
| `bo_min_atr`       | 1.0     | Minimum close distance from zone edge (x ATR)  |
| `bo_retest_max_bars`| 20     | Maximum bars to wait for a retest               |

---

## 4. Candle patterns

Detected on candle close. Precise numeric definitions (all thresholds configurable):

| Pattern          | Definition                                                             |
|------------------|------------------------------------------------------------------------|
| bullish_engulfing | Body fully covers prior candle body; close > open; prior close > prior open (bearish). |
| bearish_engulfing | Mirror of above.                                                       |
| hammer           | Lower wick = `pin_wick_ratio` × body; upper wick = body × 0.5; bullish context. |
| shooting_star    | Mirror of hammer.                                                      |
| inside_bar       | High < prior high AND low > prior low.                                 |
| outside_bar      | High > prior high AND low < prior low.                                 |
| doji             | Body = `doji_body_atr` × ATR; any wick size.                          |
| morning_star     | 3-candle: bearish, doji/small body, bullish; 3rd closes above midpoint of 1st. |
| evening_star     | Mirror of morning_star.                                                |

| Parameter         | Default | Meaning                                        |
|-------------------|---------|------------------------------------------------|
| `pin_wick_ratio`  | 2.0     | Min wick/body ratio for hammer/shooting star   |
| `doji_body_atr`   | 0.1     | Max body size for doji (x ATR)                 |

Context tags per pattern (set on confirmation):
- `at_zone`: id of the nearest S/R zone within `zone_tag_atr` × ATR, or null.
- `with_trend` / `counter_trend`: from the Trend agent's current trend direction.
- `in_killzone`: current killzone name from ICT Time agent, or null.
- `size_atr`: candle range / ATR.
- `context`: `"at_zone"` if within a zone, else `"open_space"` (lower weight).

Patterns not at any zone are stored but flagged `context = "open_space"`.

Stable ids: `pattern_{kind}:{confirmed_at}` (confirmed_at = close time of the
pattern's last candle).

---

## 5. Ranges / consolidation

An N-bar range forms when: max(high) - min(low) = `range_width_atr` × ATR over the
last `range_min_bars` to `range_max_bars` candles.

Fields: `range_high`, `range_low`, `start_time`, `end_time` (when breakout occurs),
`breakout_direction` (`up` | `down` | null while active), state (`active` | `broken`).

| Parameter       | Default | Meaning                               |
|-----------------|---------|---------------------------------------|
| `range_min_bars`| 5       | Minimum bars to qualify as a range    |
| `range_max_bars`| 50      | Maximum bars in range                 |
| `range_width_atr`| 1.5   | Maximum high-low width (x ATR)        |

Stable ids: `range:{start_time}_{start_bar_index_hash}`.

---

## 6. Round numbers

Configurable step per price scale:

| Symbol regex | Default step |
|---|---|
| `*BTC*`  | 1000 |
| `*ETH*`  | 100 |
| `*SOL*`  | 10 |
| `*BNB*`  | 10 |
| `*XRP*`  | 0.1 |
| (auto)   | nearest round power-of-10 × [1,2,5] such that 1020 levels span the last 500-bar range |

Round numbers are marked as `psychological_level` with lower weight than tested zones.
Off by default in chart overlays. Toggle to enable.

Stable ids: `round_{price_str}` (price formatted to significant figures).

---

## 7. Per-bar report

At each `bar_close` per timeframe:
```json
{
  "nearest_support": {"id": "sr_support_...", "price": 64800.0, "strength": 72, "distance_atr": -0.45},
  "nearest_resistance": {"id": "sr_resistance_...", "price": 65500.0, "strength": 85, "distance_atr": 0.73},
  "active_ranges": [{"id": "range:...", "high": 65200.0, "low": 64900.0, "bars": 12}],
  "recent_patterns": [{"id": "pattern_hammer:...", "kind": "hammer", "at_zone": "sr_support_...", "context": "at_zone", "in_killzone": "ny_am"}],
  "recent_breakouts": [{"zone_id": "...", "breakout_time": 1234567890, "retest_time": null, "outcome": null}]
}
```

---

## 8. Chart overlays

- S/R zones: box overlays with strength shading (brighter = higher strength).
- Flipped zones: distinct colour.
- Breakout/retest markers: arrowUp/arrowDown at breakout, circle at retest.
- Pattern markers: small markers, hover for details (kind, context, at_zone).
- Range boxes: shaded zone overlays.
- Round numbers: horizontal dashed lines (off by default).
- All overlays: `source_agent = "price_action"`. Toggle per concept.
- dev-sample overlay is OFF by default.

---

## Version history

| Version | Date       | Change              |
|---------|------------|---------------------|
| 1.0.0   | 2026-09-30 | Initial release     |
