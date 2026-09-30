# Deskmind Step 10 Run Plan & PC Estimates

> [!NOTE]
> **DO NOT RUN FULL PLAN YET.** This plan documents the exact CLI commands using point-in-time universe selection, resource requirements, and runtime estimates for running the full agent history backfill, feature store build, event studies, and train backtests.

---

## 1. Environment & Hardware Assumptions
* **PC Spec**: 8-core / 16-thread CPU, 32GB RAM, NVMe SSD.
* **Scope**: Top 10 point-in-time universe members on each date $\times$ 3 years (2023-01-01 $\rightarrow$ 2025-12-31) = $\approx 14$ total union symbols.
* **Bar Count**: $\approx 105,120$ 5-minute bars per symbol-year $\times 42 = 4,415,040$ 5m bars total.

---

## 2. Step-by-Step Execution Plan & Commands

### Phase 0: Point-in-Time Universe & Data Step
Build universe membership history from Binance Vision data for 2023-01-01 to 2025-12-31. Dry-run data size estimate before downloading only the required union of top N symbols.

**Commands**:
```bash
# 1. Rebuild Universe History (2023-2025)
uv run deskmind universe-history build --start 2023-01-01 --end 2025-12-31

# 2. Dry-Run Data Size & Symbol Union Estimate
uv run deskmind data-download --start 2023-01-01 --end 2025-12-31 --top-universe-n 10 --dry-run

# 3. Download Needed Klines Only
uv run deskmind data-download --start 2023-01-01 --end 2025-12-31 --top-universe-n 10
```

* **Estimated Runtime**: $\approx 10 - 15 \text{ minutes}$.
* **Data Volume**: $\approx 850 \text{ MB}$ compressed CSV/Parquet klines.

---

### Phase A: Build Agent History + Feature Store
Run batch backfills for all 4 agents (`trend`, `ict_smc`, `ict_time`, `price_action`) across point-in-time top 10 universe symbols for 2023–2025, and build 5m Parquet feature partitions.

**Command**:
```bash
uv run deskmind backfill-features \
  --start 2023-01-01 \
  --end 2025-12-31 \
  --top-universe-n 10 \
  --out-dir data/features \
  --workers 4
```

* **Estimated Runtime**: $\approx 35 - 45 \text{ minutes}$ total (across 4 parallel worker threads).
* **Storage Output**: $\approx 1.6 \text{ GB}$ of Snappy-compressed Parquet files in `data/features/`.

---

### Phase B: Fast Event Studies (H1–H5 on TRAIN split)
Run cheap forward MFE/MAE analysis (at 1, 4, 12, 48 bars) on TRAIN split (`2023-01-01 -> 2024-12-31`) for pre-registered hypotheses H1–H5 vs matched random baseline.

**Command**:
```bash
uv run deskmind run-event-study \
  --split train \
  --hypotheses H1_v1.0.0,H2_v1.0.0,H3_v1.0.0,H4_v1.0.0,H5_v1.0.0 \
  --features-dir data/features \
  --out data/research/event_studies.json \
  --baseline-samples 1000
```

* **Estimated Runtime**: $\approx 4 - 6 \text{ minutes}$.
* **Output**: Forward MFE/MAE distribution & target/stop hit ratios per symbol, per year half (`2023-H1`, `2023-H2`, `2024-H1`, `2024-H2`).

---

### Phase C: Train Backtests & Gate Evaluation
Execute full train backtests (TRAIN split: `2023-01-01 -> 2024-12-31`) for qualifying hypotheses with complete fee models (0.04% taker fee), Risk Manager, and point-in-time universe.

**Command**:
```bash
uv run deskmind run-research-backtests \
  --split train \
  --hypotheses H1_v1.0.0,H2_v1.0.0,H3_v1.0.0,H4_v1.0.0,H5_v1.0.0 \
  --out data/research/train_results.json
```

* **Estimated Runtime**: $\approx 15 - 22 \text{ minutes}$.
* **Output**: Full trade logs, gross/net expectancy in R, trial counts, Deflated Sharpe Ratio, and encoded gate evaluation checklist (`TRAIN` $\rightarrow$ `VALIDATION`).

---

## 3. Recommended Overnight Order & Pipeline Command

To run the entire pipeline autonomously overnight in sequence:

```bash
uv run deskmind run-research-pipeline \
  --start 2023-01-01 \
  --end 2025-12-31 \
  --top-universe-n 10 \
  --hypotheses H1_v1.0.0,H2_v1.0.0,H3_v1.0.0,H4_v1.0.0,H5_v1.0.0 \
  --out-dir data/research/overnight_run
```

**Total Overnight Runtime**: $\approx 65 - 85 \text{ minutes}$ complete end-to-end.
