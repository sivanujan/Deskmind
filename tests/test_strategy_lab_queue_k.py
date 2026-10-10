"""Tests for Strategy Lab stalled detection, K recount, and queue runner auto-start."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deskmind.api.app import create_app
from deskmind.strategy_lab.pipeline import (
    StrategyLabJob,
    is_queue_runner_running,
    job_to_dict,
    start_queue_runner,
    stop_queue_runner,
)
from deskmind.strategy_lab.registry import StrategyRegistry


def test_stalled_detection():
    now = time.time()

    # Job updated recently (running)
    running_active = StrategyLabJob(
        job_id="job_active",
        strategy_id="tpre_123",
        strategy_name="TPRE",
        strategy_version="1.0.0",
        strategy_path="strategies/tpre.py",
        trial_k=1,
        timeframe="5m",
        status="RUNNING_CHECKS",
        progress_pct=45.0,
        current_coin="150/364 BTCUSDT",
        last_update_ts=now - 30.0,
    )
    d_active = job_to_dict(running_active)
    assert d_active["is_stalled"] is False
    assert 29.0 <= d_active["last_update_age_s"] <= 35.0

    # Job with no update for > 120s (stalled running)
    running_stalled = StrategyLabJob(
        job_id="job_stalled",
        strategy_id="tpre_123",
        strategy_name="TPRE",
        strategy_version="1.0.0",
        strategy_path="strategies/tpre.py",
        trial_k=1,
        timeframe="5m",
        status="TRAIN_SCAN",
        progress_pct=10.0,
        current_coin="20/364 ETHUSDT",
        last_update_ts=now - 150.0,
    )
    d_stalled = job_to_dict(running_stalled)
    assert d_stalled["is_stalled"] is True
    assert d_stalled["last_update_age_s"] >= 149.0

    # Queued job older than 120s without progress when worker is stopped
    queued_stalled = StrategyLabJob(
        job_id="job_queued_stalled",
        strategy_id="tpre_123",
        strategy_name="TPRE",
        strategy_version="1.0.0",
        strategy_path="strategies/tpre.py",
        trial_k=1,
        timeframe="5m",
        status="QUEUED",
        progress_pct=0.0,
        last_update_ts=now - 200.0,
    )
    # Stop worker to ensure queued_dead triggers
    stop_queue_runner()
    d_queued = job_to_dict(queued_stalled)
    assert d_queued["is_stalled"] is True

    # Completed or failed job is never stalled even if old
    finished_job = StrategyLabJob(
        job_id="job_finished",
        strategy_id="tpre_123",
        strategy_name="TPRE",
        strategy_version="1.0.0",
        strategy_path="strategies/tpre.py",
        trial_k=1,
        timeframe="4h",
        status="COMPLETED",
        progress_pct=100.0,
        last_update_ts=now - 3600.0,
    )
    assert job_to_dict(finished_job)["is_stalled"] is False

    failed_job = StrategyLabJob(
        job_id="job_failed",
        strategy_id="tpre_123",
        strategy_name="TPRE",
        strategy_version="1.0.0",
        strategy_path="strategies/tpre.py",
        trial_k=1,
        timeframe="4h",
        status="FAILED",
        progress_pct=10.0,
        last_update_ts=now - 3600.0,
    )
    assert job_to_dict(failed_job)["is_stalled"] is False


def test_k_recount_and_deduplication(tmp_path: Path):
    reg_file = tmp_path / "registry.json"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True)
    registry = StrategyRegistry(registry_file=reg_file)

    # Initially K is 0
    assert registry.get_trial_k() == 0

    # Allocate trial for (hash1, 4h) -> K = 1
    k1 = registry.allocate_trial_k("hash1", "4h")
    assert k1 == 1
    assert registry.get_trial_k() == 1

    # Re-run same file hash on same timeframe -> should NOT increase K
    k2 = registry.allocate_trial_k("hash1", "4h")
    assert k2 == 1
    assert registry.get_trial_k() == 1

    # Different timeframe on same hash -> K = 2
    k3 = registry.allocate_trial_k("hash1", "1h")
    assert k3 == 2
    assert registry.get_trial_k() == 2

    # Different hash on 4h -> K = 3
    k4 = registry.allocate_trial_k("hash2", "4h")
    assert k4 == 3
    assert registry.get_trial_k() == 3

    # Add mock strategy registry entries
    from deskmind.strategy_lab.registry import StrategyRegistryEntry
    registry._entries = [
        StrategyRegistryEntry(
            strategy_id="s1_hash1",
            name="S1",
            version="1.0.0",
            timeframe="4h",
            sha256="hash1",
            short_hash="hash1",
            trial_k=1,
            file_path="s1.py",
            uploaded_at="2026-10-01 00:00:00 UTC",
            params={},
        ),
        StrategyRegistryEntry(
            strategy_id="s2_hash2",
            name="S2",
            version="1.0.0",
            timeframe="4h",
            sha256="hash2",
            short_hash="hash2",
            trial_k=2,
            file_path="s2.py",
            uploaded_at="2026-10-01 00:00:00 UTC",
            params={},
        ),
    ]

    # Create 5 mock jobs on disk:
    # 3 unique pairs: (hash1, 4h), (hash1, 1h), (hash2, 4h)
    mock_jobs = [
        ("j1", "s1_hash1", "4h", "2026-10-01 01:00:00 UTC", 99),
        ("j2", "s1_hash1", "4h", "2026-10-01 02:00:00 UTC", 99),  # duplicate
        ("j3", "s1_hash1", "1h", "2026-10-01 03:00:00 UTC", 99),
        ("j4", "s2_hash2", "4h", "2026-10-01 04:00:00 UTC", 99),
        ("j5", "s1_hash1", "4h", "2026-10-01 05:00:00 UTC", 99),  # duplicate
    ]
    for j_id, s_id, tf, dt, k in mock_jobs:
        jd = jobs_dir / j_id
        jd.mkdir()
        (jd / "job.json").write_text(json.dumps({
            "job_id": j_id,
            "strategy_id": s_id,
            "timeframe": tf,
            "created_at": dt,
            "trial_k": k,
        }), encoding="utf-8")

    # Clear executed_runs to test reconstruction
    registry._executed_runs = []
    registry._save()

    # Run recompute_k() -> must recount unique pairs and correct to 3
    fixed_k = registry.recompute_k(jobs_dir=jobs_dir)
    assert fixed_k == 3
    assert registry.get_trial_k() == 3

    # Check that job.json files were updated with deduplicated trial_k:
    # (hash1, 4h) -> 1
    # (hash1, 1h) -> 2
    # (hash2, 4h) -> 3
    j1_data = json.loads((jobs_dir / "j1" / "job.json").read_text(encoding="utf-8"))
    j2_data = json.loads((jobs_dir / "j2" / "job.json").read_text(encoding="utf-8"))
    j3_data = json.loads((jobs_dir / "j3" / "job.json").read_text(encoding="utf-8"))
    j4_data = json.loads((jobs_dir / "j4" / "job.json").read_text(encoding="utf-8"))
    j5_data = json.loads((jobs_dir / "j5" / "job.json").read_text(encoding="utf-8"))

    assert j1_data["trial_k"] == 1
    assert j2_data["trial_k"] == 1
    assert j3_data["trial_k"] == 2
    assert j4_data["trial_k"] == 3
    assert j5_data["trial_k"] == 1


def test_queue_runner_auto_start_with_fastapi(monkeypatch):
    # Ensure hermetic testing without running live disk jobs
    monkeypatch.setattr("deskmind.strategy_lab.pipeline.list_jobs", lambda: [])
    monkeypatch.setattr("deskmind.strategy_lab.pipeline.run_pipeline_sync", lambda *args, **kwargs: None)

    # Stop any running runner first
    stop_queue_runner(timeout=0.5)

    # Ensure queue runner starts automatically when FastAPI app starts (lifespan)
    app = create_app()
    with TestClient(app) as client:
        assert is_queue_runner_running() is True
        resp = client.get("/api/strategy-lab/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "worker" in data
        assert data["worker"]["running"] is True

    # Calling start_queue_runner and stop_queue_runner explicitly
    start_queue_runner()
    assert is_queue_runner_running() is True
    stop_queue_runner(timeout=1.0)
    assert is_queue_runner_running() is False

