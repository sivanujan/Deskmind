# ICT / SMC Liquidity agent — rule specification

Agent name `ict_smc`, version **1.2.0** (`src/deskmind/agents/ict_smc/config.py`). Deterministic
Python, no LLM. Every parameter below is a property of `IctSmcConfig`; changing any parameter
requires a new `agent_version`.

| Parameter | Default | Meaning |
|---|---|---|
| `eq_tolerance_atr` | 0.15 | Price distance between swings to qualify as Equal Highs / Lows (x ATR) |
| `pool_swing_lookback` | 15 | Number of recent swings searched to form equal highs/lows pools |
| `max_active_pools` | 10 | Max active BSL/SSL pools retained per timeframe |
| `displacement_min_atr` | 1.0 | Minimum candle range for displacement (x ATR) |
| `displacement_body_ratio` | 0.6 | Minimum candle body / range ratio for displacement |
| `fvg_min_atr` | 0.25 | Minimum gap size between candle 1 and candle 3 for FVG (x ATR) |
| `max_active_fvgs` | 10 | Max active FVGs retained per timeframe |
| `ob_max_candles` | 3 | Max consecutive opposite candles forming an Order Block |
| `max_active_obs` | 10 | Max active Order Blocks retained per timeframe |
| `max_active_breakers` | 10 | Max active Breaker Blocks retained per timeframe |
| `void_min_atr` | 1.5 | Minimum multi-candle run size for Liquidity Void (x ATR) |
| `void_max_candles` | 5 | Maximum candles in a Liquidity Void impulse run |
| `max_active_voids` | 5 | Max active Liquidity Voids retained per timeframe |
| `ote_low_pct` | 0.62 | Lower boundary of Optimal Trade Entry retracement band (62%) |
| `ote_high_pct` | 0.79 | Upper boundary of Optimal Trade Entry retracement band (79%) |
| `smt_pairs` | `(("BTCUSDT", "ETHUSDT"),)` | Aligned symbol pairs evaluated for SMT divergence |
| `zone_max_age_bars` | 300 | Active zones with no interaction expire after this many candles |
| `warmup_bars` | 1000 | Number of closed candles replayed during agent warm-up |
| `bar_memory` | 2000 | Recent candles kept in engine memory |

---

## 0. Time and the no-look-ahead rule

Candles are processed strictly one at a time in ascending time order, only after they have
**closed**. A candle with open time `t` on a timeframe of length `iv` closes at `t + iv`.

Every item produced by the agent carries two timestamps (epoch milliseconds UTC):
- `created_time` / `time` — where the item is **drawn** on the chart (e.g. open time of candle 2 for an FVG, open time of the OB candle, pivot time of the pool).
- `confirmed_at` — the close time of the candle at which the item became **knowable** without future information.

The state of the agent *as of time T* uses only items with `confirmed_at <= T`. Any state transition
occurring after `T` (e.g. a pool swept at $T_2 > T$, an FVG filled at $T_2 > T$, an OB invalidated at $T_2 > T$)
is masked so the object appears in its exact status as of time `T`.

Confirmation times per concept:
- **FVG**: confirmed at close of the 3rd candle (`C_3.close_time`).
- **Order Block (OB)**: confirmed only when the displacement and resulting BOS are confirmed (`bos.confirmed_at`).
- **Breaker / Mitigation Block**: confirmed when the candle that closes through the OB closes (`bar.close_time`).
- **Liquidity Pool**: confirmed when the swing that creates/completes the pool is confirmed by the Trend agent.
- **Sweep of a Pool**: confirmed after the sweeping candle closes (`sweep_bar.close_time`).
- **Liquidity Void**: confirmed when the impulse run pauses or a counter candle closes.
- **PD Range**: updates when a new confirmed BOS occurs (`bos.confirmed_at`).

The same incremental engine (`IctSmcEngine.on_candle`) drives live processing, history backfill, and unit tests.

---

## 1. Clean Interface with Trend Agent

The ICT/SMC agent **builds directly on the Trend agent's confirmed swings, BOS/CHoCH, and sweeps**.
It never re-detects swings separately.
- Swings: accepts confirmed `Swing` items (`swing_high`, `swing_low`) with their `price`, `pivot_time`, `confirmed_at`, and `atr`.
- Structure breaks: accepts confirmed `Break` items (`bos`, `choch`) with `direction`, `level`, `time`, and `confirmed_at`.
- Sweeps: accepts confirmed `Sweep` items with `direction`, `level`, `extreme`, `time`, and `confirmed_at`.

---

## 2. Liquidity Pools (BSL / SSL)

Liquidity pools represent areas where stop orders cluster above highs (Buy-Side Liquidity, BSL) or
below lows (Sell-Side Liquidity, SSL).

### Equal Highs / Lows (EQH / EQL)
- When a new swing high $S_2$ is confirmed at `bar.close_time`, recent unbroken swing highs $S_1$ within
  the last `pool_swing_lookback` swings are evaluated.
- If $|S_2.\text{price} - S_1.\text{price}| \le \text{eq\_tolerance\_atr} \times \text{ATR}$, an **EQH pool (BSL)**
  is formed or extended.
  - Pool level: $\max(S_1.\text{price}, S_2.\text{price})$.
  - `created_time`: $S_1.\text{pivot\_time}$.
  - `confirmed_at`: $S_2.\text{confirmed\_at}$.
- Mirror for swing lows: if $|S_2.\text{price} - S_1.\text{price}| \le \text{eq\_tolerance\_atr} \times \text{ATR}$,
  an **EQL pool (SSL)** is formed with level $\min(S_1.\text{price}, S_2.\text{price})$.

### Single Swings as Weaker Pools
- A confirmed swing that does not pair with another within tolerance forms a single-swing pool (`pool_type: "single_high"` or `"single_low"`).
  Equal high/low pools take precedence and are considered stronger.

### Pool States
- `active`: price has not reached or swept the pool level.
- `swept`: price wicks beyond the pool level but closes back inside. If the Trend agent emitted a `sweep`
  at this candle matching the pool's swing, the pool is linked to that sweep (`swept_by = sweep.id`).
- `taken`: a candle closes beyond the pool level (`close > bsl_level` for BSL, `close < ssl_level` for SSL).

---

## 3. Displacement

A displacement candle represents aggressive institutional buying or selling:
- Candle range: $\text{high} - \text{low} \ge \text{displacement\_min\_atr} \times \text{ATR}$.
- Body dominance: $|\text{close} - \text{open}| / (\text{high} - \text{low}) \ge \text{displacement\_body\_ratio}$.
- Direction: `bullish` if $\text{close} > \text{open}$, `bearish` if $\text{close} < \text{open}$.
- Displacement qualifies Order Blocks and Fair Value Gaps.

---

## 4. Fair Value Gaps (FVG) and Inversion FVGs (IFVG)

A Fair Value Gap is a 3-candle price imbalance where candle 2 displays displacement:

### Bullish FVG
- Condition: $C_{i-2}.\text{high} < C_i.\text{low}$.
- Range: $[\text{bottom} = C_{i-2}.\text{high}, \text{top} = C_i.\text{low}]$.
- Minimum gap size: $\text{top} - \text{bottom} \ge \text{fvg\_min\_atr} \times \text{ATR}$.
- Consequent Encroachment (CE): $(\text{top} + \text{bottom}) / 2.0$ (50% midpoint).
- `created_time`: $C_{i-1}.\text{open\_time}$.
- `confirmed_at`: $C_i.\text{close\_time}$.

### Bearish FVG
- Condition: $C_{i-2}.\text{low} > C_i.\text{high}$.
- Range: $[\text{bottom} = C_i.\text{high}, \text{top} = C_{i-2}.\text{low}]$.
- Minimum gap size: $\text{top} - \text{bottom} \ge \text{fvg\_min\_atr} \times \text{ATR}$.
- Consequent Encroachment (CE): $(\text{top} + \text{bottom}) / 2.0$.
- `created_time`: $C_{i-1}.\text{open\_time}$.
- `confirmed_at`: $C_i.\text{close\_time}$.

### FVG States
- `fresh`: no subsequent candle has traded into the gap $[\text{bottom}, \text{top}]$.
- `partially_filled`: a subsequent candle trades into the gap but does not close through or fill the entire range.
- `filled`: price wicks through or touches the opposite boundary ($\le \text{bottom}$ for bullish FVG, $\ge \text{top}$ for bearish FVG) without closing through.
- `inversion`: price closes through the gap ($\text{close} < \text{bottom}$ for bullish FVG, $\text{close} > \text{top}$ for bearish FVG). The gap flips into an Inversion FVG (IFVG): support flips to resistance, or resistance flips to support.

---

## 5. Order Blocks (OB), Breakers, and Mitigation Blocks

### Order Block Qualification
- **Bullish OB**: the last bearish candle (or consecutive run of up to `ob_max_candles` bearish candles)
  immediately prior to a bullish displacement move that causes a confirmed **BOS up**.
- **Bearish OB**: the last bullish candle (or consecutive run of up to `ob_max_candles` bullish candles)
  immediately prior to a bearish displacement move that causes a confirmed **BOS down**.
- Zone boundaries:
  - `top` = $\max(\text{highs of the OB candle(s)})$.
  - `bottom` = $\min(\text{lows of the OB candle(s)})$.
  - Mean threshold (MT): $(\text{top} + \text{bottom}) / 2.0$.
- **NO LOOK-AHEAD TIMING**:
  - `created_time`: open time of the OB candle.
  - `confirmed_at`: close time of the candle confirming the resulting BOS (`bos.confirmed_at`).

### OB States
- `fresh`: price has not re-entered the zone $[\text{bottom}, \text{top}]$.
- `tested`: price wicks into the zone but does not breach the 50% Mean Threshold.
- `mitigated`: price reaches or breaches the 50% Mean Threshold.
- `invalidated`: a candle closes through the invalidation boundary ($\text{close} < \text{bottom}$ for bullish OB, $\text{close} > \text{top}$ for bearish OB).

### Breaker Block vs Mitigation Block
When an OB is invalidated (closed through):
- **Breaker Block**: if a liquidity sweep occurred (price swept a pool or recent swing high/low)
  *before* the reversal that broke through the OB. The OB flips into a Breaker:
  - Bullish OB closed through downwards after sweeping BSL -> Bearish Breaker (acts as resistance).
  - Bearish OB closed through upwards after sweeping SSL -> Bullish Breaker (acts as support).
- **Mitigation Block**: if NO liquidity sweep occurred prior to the reversal (failure swing).
  The invalidated OB flips into a Mitigation Block.

---

## 6. Liquidity Voids

A Liquidity Void represents a sharp, low-overlap single-direction run spanning 2 to `void_max_candles` candles:
- Net move: $|\text{close}_{\text{end}} - \text{open}_{\text{start}}| \ge \text{void\_min\_atr} \times \text{ATR}$.
- Low-overlap metric: body coverage $\ge 0.75 \times \text{total range}$, with minimal overlapping wicks.
- Confirmed when the impulse run halts (a candle closes in the opposite direction or forms an inside/pause bar).
- Boundaries: $[\min(\text{lows}), \max(\text{highs})]$.
- States: `open` -> `rebalancing` (partially filled) -> `filled`.

---

## 7. Premium / Discount (PD) Range & OTE

The Dealing Range represents the active swing range between the origin low and high of the current trend move:
- When a confirmed **BOS up** occurs:
  - `low`: origin swing low that initiated the impulse.
  - `high`: swing high created before retracement.
- When a confirmed **BOS down** occurs:
  - `high`: origin swing high that initiated the impulse.
  - `low`: swing low created before retracement.
- Metrics:
  - `equilibrium` = $(\text{low} + \text{high}) / 2.0$.
  - `position_pct` = $(\text{close} - \text{low}) / (\text{high} - \text{low}) \times 100$.
  - `zone`: `"discount"` if $\text{close} < \text{equilibrium}$, `"premium"` if $\text{close} > \text{equilibrium}$, else `"equilibrium"`.
  - **Optimal Trade Entry (OTE)**:
    - Retracement between 62% (`ote_low_pct = 0.62`) and 79% (`ote_high_pct = 0.79`).
    - Bullish OTE (buying in discount): $[\text{high} - 0.79 \times \text{range}, \text{high} - 0.62 \times \text{range}]$.
    - Bearish OTE (selling in premium): $[\text{low} + 0.62 \times \text{range}, \text{low} + 0.79 \times \text{range}]$.
    - `in_ote`: boolean indicating whether current close is within the OTE band.
- `pd_arrays_in_zone`: active FVGs and OBs situated inside the discount zone (for bullish structure) or premium zone (for bearish structure).

---

## 8. SMT Divergence (Smart Money Technique)

Evaluates relative strength / weakness divergence between correlated asset pairs (e.g. BTCUSDT vs ETHUSDT):
- Only evaluated when both symbols have data aligned at the same candle close time.
- **Bearish SMT**: at aligned swing highs, Symbol A makes a Higher High (HH) while Symbol B makes a Lower High (LH).
- **Bullish SMT**: at aligned swing lows, Symbol A makes a Lower Low (LL) while Symbol B makes a Higher Low (HL).
- If one symbol is absent or has no data, `smt` is reported as `null`.

---

## 9. Housekeeping and Active Zone Caps

To prevent unbounded state growth and memory bloat:
- Active zones per timeframe are capped: at most `max_active_pools` pools, `max_active_fvgs` FVGs, `max_active_obs` OBs, `max_active_breakers` breakers, `max_active_voids` voids.
- An active zone with no price interaction expires after `zone_max_age_bars` closed candles (`expired_at = bar.close_time`).
- Expired and invalidated items retain their full history for replay and auditing.

---

## 10. Output Report & Overlays

### Report Structure (per symbol, per bar_close)
```json
{
  "agent": "ict_smc",
  "agent_version": "1.0.0",
  "symbol": "BTCUSDT",
  "as_of": "2026-09-30T00:00:00Z",
  "as_of_ms": 1790726400000,
  "timeframes": {
    "1h": {
      "pd_range": {
        "low": 62500.0,
        "high": 65000.0,
        "from_bos_at": 1790722800000,
        "equilibrium": 63750.0,
        "zone": "discount",
        "position_pct": 35.2,
        "in_ote": true
      },
      "liquidity": {
        "bsl": [...],
        "ssl": [...],
        "last_sweep": {...}
      },
      "fvgs": [...],
      "order_blocks": [...],
      "breakers": [...],
      "pd_arrays_in_zone": [...],
      "displacement": {...},
      "smt": {...}
    }
  }
}
```

### Chart Overlays
Overlays strictly conform to schema version 1 (`OverlayResponse`):
- **FVG / OB / Breaker / Void**: `ZoneOverlay` with semantic `kind` (`fvg`, `order_block`, `breaker`, `liquidity_void`), fill transparency, `top`, `bottom`, `start_time`, `end_time`.
- **PD Range**: `ZoneOverlay` with green tint for Discount, red tint for Premium, highlighted gold tint for OTE band, and dashed `LineOverlay` for Equilibrium line.
- **BSL / SSL**: dashed `LineOverlay` across pool span; sweeps marked with `MarkerOverlay`.
- Replay: filtering by `confirmed_at <= T` ensures accurate point-in-time rendering.

---

## 11. Point-in-time storage (since 1.2.0)
- Source of truth = items (`agent_events`, latest version per id) + every item/state change in
  `agent_item_transitions` (`at` = bar-close boundary, `ord` = emission order; first row of an
  item = full payload, later rows = changed fields only). Engine state that is not an item is
  stored as synthetic rows: `state:pd_range` (only when the range changes; position/zone/in_ote
  are re-derived from the candle close), `state:last_sweep`, `state:smt`. Each stored run starts
  with `state:baseline` + a full snapshot of everything the engines hold, so replay starts at the
  last baseline <= T.
- `history.reconstruct_state(rows, T, cfg, last_close)` rebuilds `IctSmcEngine.state()` at T
  exactly (tested against the live engine at random T). The API rebuilds reports from it.
- Full reports: live runs only, kept `ICT_SMC_REPORT_RETENTION_DAYS` (default 3). Backfills store
  items + transitions only (`--debug-report-days N` for a short debug window).

## 12. Version History
- 1.0.0 — Initial deterministic ICT/SMC liquidity and PD implementation.
- 1.1.0 — Items store the ATR used at detection; pools carry `strength` (strong = EQH/EQL);
  warm-up source OBs of in-window breakers are persisted.
- 1.2.0 — (a) A swing re-sent by the trend engine on a state change updates the stored swing and
  never forms a new pool; EQH/EQL pools need two DISTINCT swing candles (66% of 1.1.0 EQ pools
  paired a swing with itself). (b) Unique ids: OB `ob_<dir>:<created>:<confirmed>`, breaker /
  mitigation block reuse their source OB's suffix, pools list their swing times (single-swing
  ids unchanged); an identical OB from a second BOS change is not added twice. (c) FVGs are
  tagged `size_atr` (gap / ATR) and `displacement_backed` (middle candle was a displacement);
  `fvg_min_atr` stays 0.25 and detection is unchanged (the chart hides < 0.5 ATR by default).
  (d) state() summarises only the reported pools/breakers (performance, identical output).
  (e) Point-in-time storage (section 11).
