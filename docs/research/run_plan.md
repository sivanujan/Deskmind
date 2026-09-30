# Deskmind Step 10 Run Plan & PC Estimates

> [!NOTE]
> **DO NOT RUN YET.** This plan documents the exact CLI commands, resource requirements, and runtime estimates for running the full agent history backfill, feature store build, event studies, and train backtests for 10 universe symbols from 2023-01 to 2025-12.

---

## 1. Environment & Hardware Assumptions
* **PC Spec**: 8-core / 16-thread CPU, 32GB RAM, NVMe SSD.
* **Scope**: 10 universe symbols $\times$ 3 years (2023-01-01 $\rightarrow$ 2025-12-31) = 30 symbol-years.
* **Bar Count**: $\approx 105,120$ 5-minute bars per symbol-year $\times 30 = 3,153,600$ 5m bars total.

---

## 2. Step-by-Step Execution Plan & Commands

### Phase A: Build Agent History + Feature Store
Run batch backfills for all 4 agents (`trend`, `ict_smc`, `ict_time`, `price_action`) across all 10 symbols for 2023–2025, and build 5m Parquet feature partitions.

**Command**:
```bash
python -m deskmind.cli backfill-features \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,DOTUSDT \
  --start 2023-01-01 \
  --end 2025-12-31 \
  --out-dir data/features \
  --workers 4
```

* **Estimated Runtime**: $\approx 25 - 35 \text{ minutes}$ total (across 4 parallel worker threads).
* **Storage Output**: $\approx 1.2 \text{ GB}$ of Snappy-compressed Parquet files in `data/features/`.

---

### Phase B: Fast Event Studies (H1–H5)
Run cheap forward MFE/MAE analysis (at 1, 4, 12, 48 bars) for pre-registered hypotheses H1–H5 vs matched random baseline.

**Command**:
```bash
python -m deskmind.cli run-event-study \
  --hypotheses H1_v1.0.0,H2_v1.0.0,H3_v1.0.0,H4_v1.0.0,H5_v1.0.0 \
  --features-dir data/features \
  --out data/research/event_studies.json \
  --baseline-samples 1000
```

* **Estimated Runtime**: $\approx 3 - 5 \text{ minutes}$.
* **Output**: Forward MFE/MAE distribution & target/stop hit ratios per symbol, per year half (`2023-H1`, `2023-H2`, `2024-H1`, `2024-H2`, `2025-H1`, `2025-H2`).

---

### Phase C: Train Backtests & Gate Evaluation
Execute full train backtests (2023-01-01 $\rightarrow$ 2024-12-31) for qualifying hypotheses with complete fee models (0.04% taker fee), Risk Manager, and point-in-time universe.

**Command**:
```bash
python -m deskmind.cli run-research-backtests \
  --split train \
  --hypotheses H1_v1.0.0,H2_v1.0.0,H3_v1.0.0,H4_v1.0.0,H5_v1.0.0 \
  --out data/research/train_results.json
```

* **Estimated Runtime**: $\approx 12 - 18 \text{ minutes}$.
* **Output**: Full trade logs, gross/net expectancy in R, trial counts, Deflated Sharpe Ratio, and encoded gate evaluation checklist (`TRAIN` $\rightarrow$ `VALIDATION`).

---

## 3. Recommended Overnight Order & Pipeline Command

To run the entire pipeline autonomously overnight in sequence:

```bash
python -m deskmind.cli run-research-pipeline \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,DOTUSDT \
  --start 2023-01-01 \
  --end 2025-12-31 \
  --hypotheses H1_v1.0.0,H2_v1.0.0,H3_v1.0.0,H4_v1.0.0,H5_v1.0.0 \
  --out-dir data/research/overnight_run
```

**Total Overnight Runtime**: $\approx 45 - 60 \text{ minutes}$ complete end-to-end.
