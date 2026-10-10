import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from deskmind.api.app import create_app
from deskmind.config import get_settings
from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.tas_strategy import run_backtest as run_old_backtest
from deskmind.strategy_lab.base import (
    StrategyValidationError,
    load_strategy,
    run_strategy_backtest,
    validate_strategy_ast,
)
from deskmind.strategy_lab.checks import assert_strategy_passes
from deskmind.strategy_lab.pipeline import _JOBS, StrategyLabJob, run_pipeline_sync
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.rules import load_rules

TAS_STRATEGY_PATH = Path("src/deskmind/strategy_lab/strategies/tas_v1.py")
FIXTURE_2000_BARS = Path("tests/fixtures/ethusdt_4h_2000.parquet")


def test_validator_rejects_forbidden_imports():
    code_bad_os = """
import os
class Strategy:
    name = "bad"
"""
    with pytest.raises(StrategyValidationError, match="Forbidden import"):
        validate_strategy_ast(code_bad_os)

    code_bad_requests = """
from requests import get
class Strategy:
    name = "bad"
"""
    with pytest.raises(StrategyValidationError, match="Forbidden import"):
        validate_strategy_ast(code_bad_requests)


def test_validator_rejects_missing_strategy_class():
    code_no_class = """
import numpy as np
a = 1
"""
    with pytest.raises(StrategyValidationError, match="must define a 'Strategy' class"):
        validate_strategy_ast(code_no_class)


def test_validator_rejects_missing_attributes():
    code_missing_attr = """
import numpy as np
class Strategy:
    name = "Incomplete"
    # missing version, timeframe, params, indicators, on_bar
"""
    with pytest.raises(StrategyValidationError, match="missing required attributes"):
        validate_strategy_ast(code_missing_attr)


def test_validator_accepts_clean_code():
    code_clean = """
import numpy as np
import pandas as pd
import math
from deskmind.strategy_lab import ExitBracket, TradingViewBroker

class Strategy:
    name: str = "Clean"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict = {}

    def indicators(self, df):
        return df

    def on_bar(self, i, ctx, broker):
        pass
"""
    # Should not raise
    validate_strategy_ast(code_clean)


def test_tas_v1_strategy_checks_fixture():
    """Fast integrity check on the 2,000-bar ETHUSDT fixture."""
    df_fixture = pd.read_parquet(FIXTURE_2000_BARS)
    res = assert_strategy_passes(TAS_STRATEGY_PATH, df=df_fixture, tick=0.01)
    assert res["status"] == "PASSED"
    assert res["checks"]["execution"] is True
    assert res["checks"]["accounting"] is True
    assert res["checks"]["determinism"] is True
    assert res["checks"]["no_lookahead"] is True


def test_rules_v1_configuration():
    """Verify strategy_lab/rules_v1.yaml loads with exact Phase 2 criteria."""
    rules = load_rules("v1")
    assert rules.version == "v1"
    assert rules.liquidity.min_30d_median_quote_volume_usd == 20_000_000.0
    assert rules.slippage.tiered is True
    assert len(rules.slippage.tiers) == 3

    # TRAIN gates
    assert rules.train_selection.min_positions == 30
    assert rules.train_selection.min_profit_factor == 1.30
    assert rules.train_selection.min_mean_r == 0.0
    assert rules.train_selection.max_drawdown_pct == 20.0

    # VALIDATION pass
    assert rules.validation_pass.min_pooled_mean_r == 0.0
    assert rules.validation_pass.min_pooled_profit_factor == 1.20
    assert rules.validation_pass.min_profitable_cohort_pct == 60.0
    assert rules.splits.test.locked is True


def test_registry_trial_count_k_increment(tmp_path):
    """Verify registry saves file to uploads/ and increments trial count K only for new hashes."""
    reg = StrategyRegistry(
        registry_file=tmp_path / "registry.json",
        uploads_dir=tmp_path / "uploads",
    )
    assert reg.trial_count_k == 0

    code_v1 = """
import numpy as np
import pandas as pd

class Strategy:
    name: str = "StratA"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict = {}

    def indicators(self, df): return df
    def on_bar(self, i, ctx, broker): pass
"""
    entry1, is_new1 = reg.register_code(code_v1.encode("utf-8"))
    assert is_new1 is True
    assert reg.trial_count_k == 1
    assert entry1.trial_k == 1
    assert Path(entry1.file_path).exists()

    # Re-uploading the exact same file must NOT increment K
    entry1_re, is_new1_re = reg.register_code(code_v1.encode("utf-8"))
    assert is_new1_re is False
    assert reg.trial_count_k == 1
    assert entry1_re.trial_k == 1

    # Uploading a new file with different code must increment K
    code_v2 = code_v1.replace("StratA", "StratB")
    entry2, is_new2 = reg.register_code(code_v2.encode("utf-8"))
    assert is_new2 is True
    assert reg.trial_count_k == 2
    assert entry2.trial_k == 2


def test_fastapi_strategy_lab_endpoints(tmp_path):
    """Test FastAPI upload, status, and trial count tracking."""
    app = create_app(get_settings())
    client = TestClient(app)

    # 1. Status endpoint
    status_res = client.get("/api/strategy-lab/status")
    assert status_res.status_code == 200
    assert "trial_count_k" in status_res.json()

    # 2. Upload valid strategy (auto_start=false for fast unit test)
    with open(TAS_STRATEGY_PATH, "rb") as f:
        up_res = client.post(
            "/api/strategy-lab/upload?auto_start=false",
            files={"file": ("tas_v1.py", f, "text/x-python")},
        )
    assert up_res.status_code == 200
    data = up_res.json()
    assert data["name"] == "TAS"
    assert data["version"] == "1.0.0"
    assert "trial_k" in data

    # 3. Upload invalid file extension -> 400
    bad_ext_res = client.post(
        "/api/strategy-lab/upload",
        files={"file": ("notes.txt", b"plain text", "text/plain")},
    )
    assert bad_ext_res.status_code == 400

    # 4. Upload forbidden code -> 422
    bad_code = b"import os\nclass Strategy:\n    name='bad'"
    bad_code_res = client.post(
        "/api/strategy-lab/upload",
        files={"file": ("bad.py", bad_code, "text/x-python")},
    )
    assert bad_code_res.status_code == 422


def test_tas_v1_phase2_regression_exact_numbers():
    """Regression test: Phase 2 numbers reproduction.
    Expected:
      - Eligible coins: 364
      - Selected coins: 36
      - 2025 pooled mean R: +0.0651
      - 2025 pooled PF: 1.132
      - Verdict: FAIL
    """
    import sqlite3
    rules = load_rules("v1")

    # Load frozen Phase 2 database table
    conn = sqlite3.connect("data/strategy_coin_results.db")
    df = pd.read_sql("SELECT * FROM strategy_coin_results", conn)
    conn.close()

    # 1. Train split eligibility & selection
    df_train = df[df["split"] == "train"]
    eligible_count = int(df_train["is_eligible"].sum())
    assert eligible_count == 364, f"Expected 364 eligible coins, got {eligible_count}"

    sel_mask = (
        (df_train["is_eligible"])
        & (df_train["positions"] >= rules.train_selection.min_positions)
        & (df_train["profit_factor"] >= rules.train_selection.min_profit_factor)
        & (df_train["mean_r"] > rules.train_selection.min_mean_r)
        & (df_train["max_drawdown_pct"] <= rules.train_selection.max_drawdown_pct)
    )
    selected_df = df_train[sel_mask]
    assert len(selected_df) == 36, f"Expected 36 selected coins, got {len(selected_df)}"

    # 2. Validation split on selected coins (2025)
    df_val_sel = df[df["split"] == "val_selected"]
    assert len(df_val_sel) == 36

    # Weighted mean R
    pooled_mean_r = (df_val_sel["mean_r"] * df_val_sel["positions"]).sum() / df_val_sel["positions"].sum()
    assert round(pooled_mean_r, 4) == 0.0651, f"Expected pooled mean R +0.0651, got {pooled_mean_r:.4f}"

    # Pooled Profit Factor
    wins = df_val_sel["gross_profit"].sum()
    losses = df_val_sel["gross_loss"].sum()
    pooled_pf = wins / losses
    assert round(pooled_pf, 3) == 1.132, f"Expected pooled PF 1.132, got {pooled_pf:.3f}"

    # Profitable cohort breadth
    profitable_count = int((df_val_sel["net_profit"] > 0).sum())
    cohort_breadth_pct = 100.0 * profitable_count / len(df_val_sel)
    assert profitable_count == 15
    assert round(cohort_breadth_pct, 1) == 41.7

    # Verdict evaluation by rules_v1:
    # Requires: mean R > 0 (PASS), PF >= 1.20 (FAIL: 1.132 < 1.20), cohort >= 60% (FAIL: 41.7% < 60%)
    pass_mean_r = pooled_mean_r > rules.validation_pass.min_pooled_mean_r
    pass_pf = pooled_pf >= rules.validation_pass.min_pooled_profit_factor
    pass_cohort = cohort_breadth_pct >= rules.validation_pass.min_profitable_cohort_pct
    verdict = "PASS" if (pass_mean_r and pass_pf and pass_cohort) else "FAIL"

    assert bool(pass_mean_r) is True
    assert bool(pass_pf) is False
    assert bool(pass_cohort) is False
    assert verdict == "FAIL", f"Expected FAIL verdict, got {verdict}"


@pytest.mark.slow
def test_tas_v1_strategy_checks_full():
    """Verify that tas_v1 passes all four integrity checks on full ETHUSDT history."""
    res = assert_strategy_passes(TAS_STRATEGY_PATH)
    assert res["status"] == "PASSED"
    assert res["checks"]["execution"] is True
    assert res["checks"]["accounting"] is True
    assert res["checks"]["determinism"] is True
    assert res["checks"]["no_lookahead"] is True


@pytest.mark.slow
def test_tas_v1_regression_vs_old_tas():
    """Regression test: ETHUSDT 4h 2023-01-01 -> 2024-12-31 must match old TAS exactly."""
    df = load_ohlcv("ETHUSDT", "4h")
    tick = get_tick_size("ETHUSDT")

    res_old = run_old_backtest(df, tick=tick, start="2023-01-01", end="2024-12-31")
    strat = load_strategy(TAS_STRATEGY_PATH)
    res_new = run_strategy_backtest(strat, df, tick=tick, start="2023-01-01", end="2024-12-31")

    t_old = res_old["trades"]
    t_new = res_new["trades"]
    assert len(t_old) == 187
    assert len(t_new) == 187

    # Check trade columns match within 1e-9 tolerance
    for col in t_old.columns:
        assert col in t_new.columns
        if pd.api.types.is_numeric_dtype(t_old[col]):
            diff = (t_old[col] - t_new[col]).abs().max()
            assert diff <= 1e-9, f"Column {col} differs by {diff}"
        else:
            assert (t_old[col] == t_new[col]).all(), f"Column {col} string mismatch"

    # Check positions match within 1e-9 tolerance
    p_old = res_old["positions"]
    p_new = res_new["positions"]
    assert len(p_old) == 103
    assert len(p_new) == 103

    for col in p_old.columns:
        assert col in p_new.columns
        if pd.api.types.is_numeric_dtype(p_old[col]):
            diff = (p_old[col] - p_new[col]).abs().max()
            assert diff <= 1e-9, f"Pos column {col} differs by {diff}"
        else:
            assert (p_old[col] == p_new[col]).all(), f"Pos column {col} string mismatch"

    # Check equity curve
    assert (res_old["equity"] - res_new["equity"]).abs().max() <= 1e-9

    # Check key stats
    for k in ("closed_trades", "positions", "net_profit", "profit_factor", "max_drawdown", "mean_r"):
        v_old = float(res_old["stats"][k])
        v_new = float(res_new["stats"][k])
        assert abs(v_old - v_new) <= 1e-6, f"Stat {k}: old={v_old}, new={v_new}"


@pytest.mark.slow
def test_strategy_lab_sanity_synthetic_pass_fail(tmp_path):
    """SANITY TEST for Strategy Lab: prove it can say PASS, not only FAIL.

    1. Generate 50 synthetic coins (4h bars, 2022-12-01 -> 2025-12-31) in a separate test folder.
       Random-walk prices with planted edge: after 3 consecutive up-closes, next bar closes
       up with 60% prob (mirror for down).
       Give each coin $100M daily volume and a tick size so liquidity rules pass.
    2. Strategy A (exploits edge): after 3 up-closes go long, after 3 down-closes go short;
       exit after 1 bar; stop 2 ATR.
    3. Strategy B (no edge): enter long/short at random (fixed seed), same exits.
    4. Run both through the FULL normal pipeline (checks, TRAIN selection, 2025 validation).
    Expected: A = PASS, B = FAIL.
    """
    data_dir = tmp_path / "parquet"
    data_dir.mkdir(parents=True, exist_ok=True)

    symbols = [f"SYNTH{i:02d}USDT" for i in range(50)]
    daily_records = []
    meta_dict = {}

    times = pd.date_range("2022-12-01 00:00:00", "2025-12-31 20:00:00", freq="4h", tz="UTC")
    n = len(times)
    dates_unique = sorted(list({t.strftime("%Y-%m-%d") for t in times}))

    for idx, sym in enumerate(symbols):
        rng = np.random.RandomState(4200 + idx)
        initial_price = rng.uniform(80.0, 120.0)

        opens = np.empty(n, dtype=np.float64)
        highs = np.empty(n, dtype=np.float64)
        lows = np.empty(n, dtype=np.float64)
        closes = np.empty(n, dtype=np.float64)
        volumes = np.empty(n, dtype=np.float64)
        quote_volumes = np.full(n, 100_000_000.0 / 6.0, dtype=np.float64)

        current_close = initial_price
        directions = []

        for i in range(n):
            open_p = current_close
            opens[i] = open_p

            p_up = 0.50
            if len(directions) >= 3:
                if directions[-1] == 1 and directions[-2] == 1 and directions[-3] == 1:
                    p_up = 0.60
                elif directions[-1] == -1 and directions[-2] == -1 and directions[-3] == -1:
                    p_up = 0.40

            is_up = rng.rand() < p_up
            dir_val = 1 if is_up else -1
            directions.append(dir_val)

            ret_mag = rng.uniform(0.026, 0.034)
            if is_up:
                close_p = open_p * np.exp(ret_mag)
                high_p = close_p * (1.0 + rng.uniform(0.001, 0.002))
                low_p = open_p * (1.0 - rng.uniform(0.001, 0.002))
            else:
                close_p = open_p * np.exp(-ret_mag)
                high_p = open_p * (1.0 + rng.uniform(0.001, 0.002))
                low_p = close_p * (1.0 - rng.uniform(0.001, 0.002))

            closes[i] = close_p
            highs[i] = high_p
            lows[i] = low_p
            current_close = close_p
            volumes[i] = (100_000_000.0 / 6.0) / close_p

        df_coin = pd.DataFrame({
            "open_time": times,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "quote_volume": quote_volumes,
        })

        coin_klines_dir = data_dir / sym / "klines" / "4h"
        coin_klines_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(df_coin), str(coin_klines_dir / "data.parquet"))

        meta_dict[sym] = {"symbol": sym, "tick_size": "0.0001"}

        for d in dates_unique:
            daily_records.append({"symbol": sym, "date": d, "quote_volume": 100_000_000.0})

    meta_path = tmp_path / "symbol_filters.json"
    meta_path.write_text(json.dumps(meta_dict, indent=2), encoding="utf-8")

    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps([{"symbol": s} for s in symbols], indent=2), encoding="utf-8"
    )

    daily_vol_path = tmp_path / "daily_quote_volume.parquet"
    pd.DataFrame(daily_records).to_parquet(str(daily_vol_path), index=False)

    strat_a_code = '''from __future__ import annotations
from typing import Any
import numpy as np
import pandas as pd
from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr

class Strategy:
    name: str = "StrategyA_Edge"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {
        "atr_len": 14,
        "stop_atr_mult": 2.0,
        "initial_capital": 10_000.0,
        "equity_pct_per_trade": 10.0,
        "commission_pct": 0.05,
        "slippage_ticks": 1,
    }

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        df = df.copy()
        df["atr"] = atr(h, l, c, self.params["atr_len"])

        up = c > np.roll(c, 1)
        up[0] = False
        down = c < np.roll(c, 1)
        down[0] = False

        up3 = up & np.roll(up, 1) & np.roll(up, 2)
        up3[:3] = False
        down3 = down & np.roll(down, 1) & np.roll(down, 2)
        down3[:3] = False

        df["up3"] = up3
        df["down3"] = down3
        return df

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        if broker.position is not None:
            broker.close("1-bar exit")
            return

        d = ctx.df
        up3 = bool(d["up3"].iloc[i])
        down3 = bool(d["down3"].iloc[i])
        bar_atr = float(d["atr"].iloc[i])

        if not ctx.in_date or np.isnan(bar_atr) or bar_atr <= 0:
            return

        stop_dist = self.params["stop_atr_mult"] * bar_atr
        if up3:
            broker.entry(side=1, stop=ctx.close - stop_dist, current_close=ctx.close)
        elif down3:
            broker.entry(side=-1, stop=ctx.close + stop_dist, current_close=ctx.close)
'''

    strat_b_code = '''from __future__ import annotations
from typing import Any
import numpy as np
import pandas as pd
from deskmind.strategy_lab.base import BarContext
from deskmind.strategy_lab.broker import TradingViewBroker
from deskmind.strategy_lab.indicators import atr

class Strategy:
    name: str = "StrategyB_NoEdge"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict[str, Any] = {
        "atr_len": 14,
        "stop_atr_mult": 2.0,
        "initial_capital": 10_000.0,
        "equity_pct_per_trade": 10.0,
        "commission_pct": 0.05,
        "slippage_ticks": 1,
    }

    def __init__(self, **kwargs: Any) -> None:
        self.params = {**self.params, **kwargs}

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        df = df.copy()
        df["atr"] = atr(h, l, c, self.params["atr_len"])

        rng = np.random.RandomState(42)
        entry_choice = rng.choice([0, 1, -1], p=[0.75, 0.125, 0.125], size=len(df))
        df["entry_sig"] = entry_choice
        return df

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        if broker.position is not None:
            broker.close("1-bar exit")
            return

        d = ctx.df
        sig = int(d["entry_sig"].iloc[i])
        bar_atr = float(d["atr"].iloc[i])

        if not ctx.in_date or np.isnan(bar_atr) or bar_atr <= 0 or sig == 0:
            return

        stop_dist = self.params["stop_atr_mult"] * bar_atr
        if sig == 1:
            broker.entry(side=1, stop=ctx.close - stop_dist, current_close=ctx.close)
        elif sig == -1:
            broker.entry(side=-1, stop=ctx.close + stop_dist, current_close=ctx.close)
'''

    strat_a_path = tmp_path / "strat_a.py"
    strat_a_path.write_text(strat_a_code, encoding="utf-8")
    strat_b_path = tmp_path / "strat_b.py"
    strat_b_path.write_text(strat_b_code, encoding="utf-8")

    # Load frozen rules_v1 and point universe and volume paths to synthetic data
    rules = load_rules("v1")
    rules.universe.path = str(universe_path)
    rules.liquidity.volume_data_path = str(daily_vol_path)

    job_id_a = "test_sanity_a"
    job_id_b = "test_sanity_b"

    try:
        job_a = StrategyLabJob(
            job_id=job_id_a,
            strategy_id="strat_a",
            strategy_name="StrategyA_Edge",
            strategy_version="1.0.0",
            strategy_path=str(strat_a_path),
            trial_k=0,
            timeframe="4h",
        )
        _JOBS[job_id_a] = job_a
        run_pipeline_sync(
            job_id_a,
            rules_version=rules,
            data_dir=data_dir,
            meta_path=meta_path,
            benchmark_symbol=symbols[0],
        )

        job_b = StrategyLabJob(
            job_id=job_id_b,
            strategy_id="strat_b",
            strategy_name="StrategyB_NoEdge",
            strategy_version="1.0.0",
            strategy_path=str(strat_b_path),
            trial_k=0,
            timeframe="4h",
        )
        _JOBS[job_id_b] = job_b
        run_pipeline_sync(
            job_id_b,
            rules_version=rules,
            data_dir=data_dir,
            meta_path=meta_path,
            benchmark_symbol=symbols[0],
        )

        # Assertions
        assert job_a.verdict == "PASS", (
            f"Strategy A expected PASS, got {job_a.verdict}. Rules: {job_a.rules_verdict}"
        )
        assert job_b.verdict == "FAIL", (
            f"Strategy B expected FAIL, got {job_b.verdict}. Rules: {job_b.rules_verdict}"
        )

        # Strategy A gate checks
        assert job_a.rules_verdict["selected_coins"]["passed"] is True
        assert job_a.rules_verdict["min_val_pooled_mean_r"]["passed"] is True
        assert job_a.rules_verdict["min_val_pooled_pf"]["passed"] is True
        assert job_a.rules_verdict["pct_selected_profitable"]["passed"] is True

        # Strategy B fail check
        assert job_b.rules_verdict["selected_coins"]["passed"] is False

    finally:
        for j_id in [job_id_a, job_id_b]:
            _JOBS.pop(j_id, None)
            j_dir = Path("data/strategy_lab/jobs") / j_id
            if j_dir.exists():
                shutil.rmtree(j_dir, ignore_errors=True)

