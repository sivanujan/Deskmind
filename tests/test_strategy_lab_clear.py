"""Strategy Lab "Clear everything", cancellation of a running job and K never going down."""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmind.strategy_lab import pipeline
from deskmind.strategy_lab.registry import StrategyRegistry, StrategyRegistryEntry


def _entry(sha: str) -> StrategyRegistryEntry:
    return StrategyRegistryEntry(
        strategy_id=f"S_{sha[:8]}",
        name="S",
        version="1.0.0",
        timeframe="4h",
        sha256=sha,
        short_hash=sha[:8],
        trial_k=0,
        file_path="uploads/s.py",
        uploaded_at="2026-10-01 00:00:00 UTC",
        params={},
    )


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run in an empty working dir (pipeline uses relative data/ paths) with no worker thread."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, "start_queue_runner", lambda: None)
    pipeline._JOBS.clear()
    pipeline._JOB_QUEUE.clear()
    yield tmp_path
    pipeline._JOBS.clear()
    pipeline._JOB_QUEUE.clear()


def test_clear_removes_jobs_and_strategies_but_keeps_k(lab: Path) -> None:
    reg = StrategyRegistry()
    reg._entries = [_entry("a" * 64)]
    reg._save()
    jobs = pipeline.start_pipeline_jobs(_entry("a" * 64), timeframes=["4h", "1h"])
    assert len(jobs) == 2
    assert StrategyRegistry().trial_count_k == 2
    assert (lab / "data/strategy_lab/jobs" / jobs[0].job_id / "job.json").exists()

    removed = pipeline.clear_all_jobs()

    assert removed == 2
    assert pipeline.list_jobs() == []
    assert pipeline._JOB_QUEUE == []
    assert not (lab / "data/strategy_lab/jobs").exists()
    after = StrategyRegistry()
    assert after.get_entries() == []
    assert after.trial_count_k == 2  # K counts every trial ever run
    # Recomputing K from the (now empty) job files must not lower it either.
    assert after.recompute_k() == 2


def test_cleared_job_is_never_written_back(lab: Path) -> None:
    job = pipeline.start_pipeline_jobs(_entry("b" * 64), timeframes=["4h"])[0]
    pipeline.clear_all_jobs()
    pipeline.save_job(job)  # a pipeline still holding the object tries to save
    pipeline.append_job_log(job.job_id, "late line")
    assert not (lab / "data/strategy_lab/jobs" / job.job_id).exists()


def test_cancel_stops_a_running_pipeline(lab: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline checks for cancellation at every progress update and stops there."""
    job = pipeline.start_pipeline_jobs(_entry("c" * 64), timeframes=["4h"])[0]
    seen: list[str] = []

    def fake_checks(_path: Path, **_kw) -> dict:
        seen.append("checks")
        pipeline.cancel_job(job.job_id)  # user cancels while the job runs
        return {"status": "PASSED"}

    def must_not_run(*_a, **_k):
        raise AssertionError("pipeline kept running after cancel")

    monkeypatch.setattr(pipeline, "build_volume_map", lambda _p: {})
    monkeypatch.setattr(pipeline, "load_universe_symbols", lambda _p: ["AUSDT"])
    monkeypatch.setattr(pipeline, "run_strategy_checks", fake_checks)
    monkeypatch.setattr(pipeline, "load_strategy", must_not_run)

    pipeline.run_pipeline_sync(job.job_id)

    assert seen == ["checks"]
    assert pipeline.get_job(job.job_id).status == "CANCELLED"


def _fake_scan(sym, tf, path, start, end, *_rest):
    """Coin result shaped like workers.scan_symbol_worker. GOOD passes every TRAIN rule."""
    good = sym.startswith("GOOD")
    in_2025 = start.startswith("2025")
    return {
        "symbol": sym,
        "is_eligible": True,
        "positions": 40 if good else 5,
        "closed_trades": 40 if good else 5,
        "profit_factor": 1.6 if good else 0.8,
        "mean_r": 0.2 if good else -0.1,
        "max_drawdown_pct": 12.0,
        "net_profit": 50.0 if (good and in_2025) else -10.0,
        "gross_profit": 150.0 if good else 40.0,
        "gross_loss": 100.0 if not good else 60.0,
        "positions_records": [{"r_multiple": 0.2 if good else -0.1, "cost_in_r": 0.05}],
    }


def test_pipeline_completes_after_train_scan(lab: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: jobs crashed at 65% (TRAIN selection) on keys missing from the rules/results."""
    from concurrent.futures import ThreadPoolExecutor

    job = pipeline.start_pipeline_jobs(_entry("e" * 64), timeframes=["4h"])[0]
    monkeypatch.setattr(pipeline, "run_strategy_checks", lambda _p: {"status": "PASSED"})
    monkeypatch.setattr(pipeline, "load_strategy", lambda _p: type("S", (), {"timeframe": "4h"})())
    monkeypatch.setattr(pipeline, "build_volume_map", lambda _p: {})
    coins = ["GOODUSDT", "BADUSDT", "GOOD2USDT"]
    monkeypatch.setattr(pipeline, "load_universe_symbols", lambda _p: coins)

    def no_data(*_a):
        raise OSError("no data")  # benchmark timing falls back to its default

    monkeypatch.setattr(pipeline, "load_ohlcv", no_data)
    monkeypatch.setattr(pipeline, "_scan_pool", lambda n: ThreadPoolExecutor(max_workers=n))
    monkeypatch.setattr(pipeline, "scan_symbol_worker", _fake_scan)

    pipeline.run_pipeline_sync(job.job_id)

    done = pipeline.get_job(job.job_id)
    assert done.status == "COMPLETED", done.reject_reason
    assert done.selected_symbols == ["GOODUSDT", "GOOD2USDT"]
    assert done.funnel.selected_coins == 2 and done.funnel.profitable_2025 == 2
    assert done.not_tradeable[0]["symbol"] == "BADUSDT"
    assert "positions 5 < 30" in done.not_tradeable[0]["reason"]
    assert set(done.rules_verdict) == {
        "selected_coins",
        "min_val_pooled_mean_r",
        "min_val_pooled_pf",
        "pct_selected_profitable",
    }
    assert done.verdict == "PASS"
    assert (done.coins_done, done.coins_total) == (2, 2)  # last phase: 2025 on picked coins


def test_new_job_reports_coin_progress_fields(lab: Path) -> None:
    job = pipeline.start_pipeline_jobs(_entry("d" * 64), timeframes=["4h"])[0]
    d = pipeline.job_to_dict(job)
    assert (d["coins_done"], d["coins_total"], d["scan_phase"]) == (0, 0, "")


def test_scan_worker_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto = half the CPU threads, at most 6 (all cores froze the PC); config overrides."""
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(strategy_lab_workers=0))
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 16)
    assert pipeline.scan_worker_count() == 6
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 4)
    assert pipeline.scan_worker_count() == 2
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 1)
    assert pipeline.scan_worker_count() == 1
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(strategy_lab_workers=3))
    assert pipeline.scan_worker_count() == 3


def test_scan_pool_runs_low_priority_workers() -> None:
    with pipeline._scan_pool(2) as pool:
        assert list(pool.map(abs, [-1, -2, -3])) == [1, 2, 3]


def test_status_summary_leaves_out_per_coin_lists(lab: Path) -> None:
    job = pipeline.start_pipeline_jobs(_entry("f" * 64), timeframes=["4h"])[0]
    job.per_coin_results = [{"symbol": f"C{i}USDT"} for i in range(620)]
    job.selected_symbols = ["AUSDT", "BUSDT"]
    d = pipeline.job_summary_dict(job)
    for key in ("per_coin_results", "candidate_coins", "not_tradeable", "selected_symbols"):
        assert key not in d
    assert d["selected_count"] == 2
    assert d["status"] == "QUEUED" and "funnel" in d and "comparison_row" in d


def test_scan_pool_survives_more_tasks_than_old_recycle_limit() -> None:
    """Regression: with max_tasks_per_child=25 every scan hung at workers x 25 coins (150)."""
    from concurrent.futures import wait

    with pipeline._scan_pool(2) as pool:
        futures = [pool.submit(abs, -i) for i in range(120)]  # > 2 workers x 25 tasks
        done, not_done = wait(futures, timeout=120)
    assert not not_done
    assert sorted(f.result() for f in done) == list(range(120))


def test_hung_coin_fails_the_scan_instead_of_stalling(monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    def slow_worker(sym, *_args):
        if sym == "HANGUSDT":
            time.sleep(2)
        return {"symbol": sym}

    monkeypatch.setattr(pipeline, "_scan_pool", lambda n: ThreadPoolExecutor(max_workers=n))
    monkeypatch.setattr(pipeline, "scan_symbol_worker", slow_worker)
    monkeypatch.setattr(pipeline, "COIN_RESULT_TIMEOUT_S", 0.3)
    seen: list[str] = []
    with pytest.raises(RuntimeError, match="HANGUSDT"):
        pipeline._scan_coins(2, ["AUSDT", "HANGUSDT"], (), lambda i, r: seen.append(r["symbol"]))
    assert seen == ["AUSDT"]


def test_library_lists_every_builtin_strategy_without_errors() -> None:
    from deskmind.strategy_lab import api as lab_api

    items = {i["file"]: i for i in lab_api.get_strategy_library()}
    expected = {
        "sma_crossover_v1",
        "ema_crossover_v1",
        "rsi_reversal_v1",
        "macd_v1",
        "bollinger_bands_v1",
        "breakout_v1",
        "mean_reversion_v1",
        "momentum_v1",
        "tas_v1",
    }
    assert expected <= set(items)
    for stem in expected:
        assert "error" not in items[stem], items[stem]
        assert items[stem]["title"] and items[stem]["params"]
    assert items["sma_crossover_v1"]["params"]["sl_atr_mult"] == 2.0


def test_library_register_only_accepts_listed_names(lab: Path) -> None:
    from fastapi import HTTPException

    from deskmind.strategy_lab import api as lab_api

    with pytest.raises(HTTPException) as e:
        lab_api.register_library_strategy("../registry")
    assert e.value.status_code == 404
    res = lab_api.register_library_strategy("macd_v1")
    assert res.name == "MACD" and res.is_new_upload
    assert lab_api.register_library_strategy("macd_v1").is_new_upload is False  # same file


def test_ported_strategies_only_trade_next_bar_with_a_stop() -> None:
    """Signals at bar i fill at bar i+1's open and every position carries a stop below/above."""
    import numpy as np
    import pandas as pd

    from deskmind.strategy_lab.base import load_strategy, run_strategy_backtest

    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1.5, 1500))
    df = pd.DataFrame(
        {
            "open_time": pd.date_range("2023-01-01", periods=1500, freq="4h", tz="UTC"),
            "open": np.r_[close[0], close[:-1]],
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        }
    )
    root = Path("src/deskmind/strategy_lab/strategies")
    for stem in ("sma_crossover_v1", "rsi_reversal_v1", "bollinger_bands_v1", "momentum_v1"):
        res = run_strategy_backtest(load_strategy(root / f"{stem}.py"), df, tick=0.01)
        pos = res["positions"]
        assert len(pos) > 0, stem
        assert (pos["risk_unit"] > 0).all(), stem  # a stop distance on every position
