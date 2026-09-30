# ICT Time agent — rule specification

Agent name `ict_time`, version **1.0.0** (`src/deskmind/agents/ict_time/config.py`).
Deterministic Python, no LLM. Timeframes: 5m, 15m, 1h, 2h (per symbol).

---

## 0. No-look-ahead rule

Every item carries `created_time` and `confirmed_at` (epoch ms UTC). As-of queries
use only items with `confirmed_at <= T`. One code path for live and batch.

---

## 1. Trading Sessions (America/New_York)

Sessions are defined in local New York wall-clock time (`America/New_York`):
- **Asia**: 20:00 – 00:00 NY (open on previous calendar day)
- **London**: 02:00 – 05:00 NY
- **New York**: 08:00 – 17:00 NY

### DST Spring Transition (Skipped Hour 02:00 NY)
On US DST start (second Sunday in March, e.g. `2026-03-08`), local wall-clock time jumps from `01:59:59 EST` straight to `03:00:00 EDT`. The local hour `02:00 NY` does not exist on that day.
- **London Session Behavior**: London session start is defined by the 02:00 NY local clock event. On DST start day, London session open occurs at `07:00 UTC` (which maps to `03:00 EDT` in local New York time as EDT is UTC-4).

---

## 2. Killzones & Silver Bullets

All windows evaluated against New York local clock time:
- **Asian Killzone**: 20:00 – 00:00 NY
- **London Open Killzone**: 02:00 – 05:00 NY
- **NY AM Killzone**: 08:30 – 11:00 NY
- **NY Lunch**: 12:00 – 13:00 NY
- **London Close Killzone**: 10:00 – 12:00 NY

Silver Bullets:
- **SB1 (London)**: 03:00 – 04:00 NY
- **SB2 (NY AM)**: 10:00 – 11:00 NY
- **SB3 (NY PM)**: 14:00 – 15:00 NY

---

## 3. Key Levels (Day Rollover at 00:00 NY)

Emitted at daily rollover (00:00 NY midnight):
- `pdh` / `pdl`: Previous Day High / Low
- `pwh` / `pwl`: Previous Week High / Low
- `pmh` / `pml`: Previous Month High / Low
- `daily_open`: 00:00 NY bar open price
- `ny_midnight_open`: 00:00 NY bar open price
- `asian_range_high` / `asian_range_low`: High / Low during Asia session (20:00 - 00:00 NY)

---

## 4. AMD Heuristic (Accumulation / Manipulation / Distribution)

- **Accumulation**: During Asia session
- **Manipulation**: During London session (if Asia high/low swept)
- **Distribution**: During NY session
