# Trend & Structure agent â rule specification

Agent name `trend`, version **1.2.0** (`src/deskmind/agents/trend/config.py`). Deterministic
Python, no LLM. Every number below is a parameter of `TrendConfig`; changing any parameter
requires a new `agent_version`.

| Parameter | Default | Meaning |
|---|---|---|
| `left_bars` / `right_bars` | 3 / 3 | fractal window for pivots |
| `atr_period` | 14 | ATR window (simple moving average of true range) |
| `min_swing_atr` | 1.0 | minimum swing size, in ATR |
| `trendline_tolerance_atr` | 0.25 | touch / break tolerance of trend lines, in ATR |
| `trendline_min_touches` | 3 | touches for a trend line to exist |
| `trendline_pivot_window` | 8 | recent swings considered as trend-line anchors |
| `max_active_trendlines` | 10 | per side; the oldest (by last touch) expires beyond this |
| `trendline_max_idle_bars` | 150 | a line with no new touch for this many candles expires |
| `range_after_bars` | 100 | no structure break for this many bars -> trend `ranging` |
| `warmup_bars` | 1000 | history replayed before live processing |

## 0. Time and the no-look-ahead rule

Candles are processed one at a time, strictly in time order, only after they have **closed**.
A candle with open time `t` on a timeframe of length `iv` closes at `t + iv`.

Every item the agent produces carries two times:

- `pivot_time` / `time` â where it is **drawn** (the candle open time of the high/low or break).
- `confirmed_at` â the close time of the candle at which the item became **knowable**.

The state of the agent *as of time T* uses only items with `confirmed_at <= T`. Later changes to
an item (a swing getting broken, a trend line gaining a touch or breaking) carry their own time
(`broken_at`, touch `confirmed_at`, `expired_at`) and are also hidden when they are after `T`.
The same incremental engine (`TrendEngine.on_candle`) is used for live processing, history
backfill and tests; there is no separate vectorised implementation.

## 1. ATR

True range of candle `i`: `max(high - low, |high - prev_close|, |low - prev_close|)`
(`high - low` for the first candle). `ATR_i` = simple average of the last `atr_period` true
ranges including candle `i`. No ATR (and therefore no swings or trend lines) before
`atr_period` candles. A simple (not Wilder) average is used on purpose: it depends only on the
last 14 candles, so results do not depend on where the history starts.

## 2. Swing highs / lows (fractal pivots + size filter)

Candidate at candle `p`, evaluated when candle `i = p + right_bars` closes
(`confirmed_at = close_time(i)`):

- **pivot high**: `high[p] > high[j]` for the `left_bars` candles before `p`, and
  `high[p] >= high[j]` for the `right_bars` candles after `p` (strict left, non-strict right:
  of two equal highs the first one is the pivot).
- **pivot low**: mirror image with lows.
- A candle can be both (outside bar); the high is processed first.

**Size filter** (ATR = `ATR_i`, known at confirmation): let `q` be the pivot candle of the
previously accepted swing (either side), or `p - left_bars` if there is none. A pivot high is
accepted if `high[p] - min(low over candles q+1 .. p) >= min_swing_atr * ATR`; a pivot low if
`max(high over candles q+1 .. p) - low[p] >= min_swing_atr * ATR`. Rejected candidates are
dropped (not stored).

## 3. Structure labels

On acceptance a swing high is compared with the previous accepted swing high:
`HH` if higher, otherwise `LH` (an equal high is not a higher high). A swing low is compared with
the previous accepted swing low: `LL` if lower, otherwise `HL` (an equal low still holds).
The first swing of each side has no label (`H` / `L` on the chart).

## 4. Structure breaks: BOS, CHoCH, sweep

The **level** of each side is the most recent accepted swing of that side that has not been
broken yet (a newer swing replaces the level even if the older one was never broken). For every
closed candle, before new pivots are evaluated:

- Up side: if `high > level_high`:
  - `close > level_high` -> **break up** at this candle;
  - otherwise (wick only) -> **sweep** up (level stays unbroken; recorded every candle it happens).
- Down side: mirror image with `low < level_low` / `close < level_low`.

A break is a **BOS** when it goes in the current trend direction or the trend is `ranging`, and a
**CHoCH** when it goes against the current trend. After a break: the swing is marked broken
(`broken_at` = close time of the break candle), the level of that side is empty until the next
swing of that side is accepted, and the trend becomes the break direction.

Break fields: `type` (BOS/CHoCH), `direction` (up/down), `level`, `time` (break candle open),
`confirmed_at` (break candle close), `swing_pivot_time`.

## 5. Trend state

- `bullish` after an up break, `bearish` after a down break, `ranging` before the first break.
- If `range_after_bars` candles close without any break, the trend becomes `ranging`.
- Machine-readable reason: `bos_up`, `bos_down`, `choch_up`, `choch_down`, `no_break_yet`,
  `no_break_for_<N>_bars`.

## 6. Trend lines

- **Support** lines connect accepted swing lows with a rising slope; **resistance** lines connect
  accepted swing highs with a falling slope. Line value at time `t`:
  `price_a + slope * (t - time_a)`, `slope = (price_b - price_a) / (time_b - time_a)`.
- Tolerance `tol = trendline_tolerance_atr * ATR` at the moment the line is created (fixed for
  the life of the line).
- When a new swing low `s` is accepted (highs: mirror image):
  1. it adds a **touch** to every active support line with `|line(s) - s.price| <= tol`;
  2. if it touched no active line, candidate lines are formed from every pair `(a, b)` of the
     last `trendline_pivot_window` swing lows (a older than b, `price_b > price_a`). A candidate
     is **valid** when `s` is within `tol` of the line, the touches (swing lows from `a` to `s`
     within `tol`) are `>= trendline_min_touches`, and **no candle from `a` to the current
     candle closed below `line - tol`** (above `line + tol` for resistance). Of the valid
     candidates the one with most touches (then the earliest `a`) becomes a new line with
     `confirmed_at` = confirmation of `s`. Two touches never make a line. A line defined by the
     same two anchors as any earlier line (active, broken or expired) is never created again.
- An active support line is **broken** when a candle closes below `line - tol` (resistance: above
  `line + tol`): `broken_at` = that close time. Broken lines stay on the chart, dimmed.
- A line expires (`expired_at` = that close time) when `trendline_max_idle_bars` candles have
  closed since its newest touch (checked each candle, before new pivots). Expired lines stay on the
  chart, dimmed, and no longer take touches.
- At most `max_active_trendlines` per side; beyond that the one with the oldest last touch
  expires (`expired_at`).
- `active_trendline` in the report: bullish -> best active support, bearish -> best active
  resistance, ranging -> the active line with the most recent touch. Best = most touches, then
  most recent touch.

## 7. Output

Per symbol, after every `bar_close` event, a report (stored in Postgres `agent_reports`,
published on Redis `deskmind:agent_report`):

```json
{"agent": "trend", "agent_version": "1.0.0", "symbol": "BTCUSDT", "as_of": "...Z", "as_of_ms": 0,
 "timeframes": {"15m": {"as_of": "...", "trend": "bullish", "reason": "bos_up",
   "last_swing_high": {"price": 0, "label": "HH", "pivot_time": "...", "confirmed_at": "..."},
   "last_swing_low": {...}, "last_event": {"type": "BOS", "direction": "up", "level": 0,
   "time": "...", "confirmed_at": "..."}, "active_trendline": {...} | null, "atr": 0}}}
```

Only timeframes that closed at `as_of` are updated; the others keep their latest state with their
own `as_of`. Items (swings, breaks, sweeps, trend lines) are stored in `trend_items` and served
to the chart by `GET /api/overlays` (`as_of` parameter = replay).

Point-in-time storage (since 1.2.0, same design as ict_smc): items + every change in
`agent_item_transitions` (compressed TimescaleDB hypertables), plus the synthetic `state:trend`
({trend, reason}; it also changes on a timeout to ranging, without an item). `as_of` and `atr`
move every candle and are re-derived from the candles at T (`history.bar_context`, the engine's
formula). `history.reconstruct_state` == `TrendEngine.state()` at any T (tested at random T).
Backfills store no reports; live runs keep them `TREND_REPORT_RETENTION_DAYS` (default 3).

## 8. Version history

- 1.0.0 â initial rules.
- 1.1.0 â trend lines expire after `trendline_max_idle_bars` without a touch. Reason: in 1.0.0 a
  line that was never broken stayed active forever, so where a run started changed which lines
  existed (measured: up to 632 candles to converge on BNBUSDT 2h). Stored items and reports are
  keyed by version; the API only serves the current version.
- 1.1.1 â an expired line could be re-created later from the same two anchors, re-using its id
  with a later `confirmed_at`; the stored item then no longer showed that the line was known
  earlier. Found by the real-data no-look-ahead test (SOLUSDT 5m). Line anchors are now used at
  most once.
- 1.2.0 â storage only, rules unchanged: items + state transitions are the point-in-time record
  (section 7); backfills store no full reports; live reports have a retention window.

## 9. Warm-up and consistency

The live agent replays the last `warmup_bars` candles of each timeframe before processing
events; items from the older half of the warm-up are not written (they already exist from the
backfill or earlier runs). The structure state only depends on recent swings, so two runs that
start at different points agree after a short convergence period; the check
`deskmind agents check trend` measures this and the no-look-ahead property on real data.
