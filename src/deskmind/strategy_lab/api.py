"""FastAPI endpoints for Strategy Lab Phase B and Multi-Timeframe Support."""

from __future__ import annotations

import ast
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from deskmind.strategy_lab.base import StrategyValidationError, load_strategy
from deskmind.strategy_lab.pipeline import (
    StrategyLabJob,
    cancel_job,
    clear_all_jobs,
    export_comparison_report_csv,
    get_comparison_report,
    get_job,
    get_job_log_lines,
    get_worker_status,
    job_summary_dict,
    job_to_dict,
    list_jobs,
    retry_job,
    start_pipeline_jobs,
    start_queue_runner,
)
from deskmind.strategy_lab.registry import StrategyRegistry

router = APIRouter(prefix="/api/strategy-lab", tags=["Strategy Lab"])


class UploadResponse(BaseModel):
    strategy_id: str
    name: str
    version: str
    timeframe: str
    sha256: str
    short_hash: str
    trial_k: int
    file_path: str
    uploaded_at: str
    is_new_upload: bool
    job_id: str | None = None
    job_ids: list[str] = []


class JobStartRequest(BaseModel):
    strategy_id: str
    rules_version: str = "v1"
    timeframes: list[str] | None = None
    eval_mode: str = "standard"


class Test2026Request(BaseModel):
    confirm: bool = False


@router.get("/status")
def get_strategy_lab_status() -> dict[str, Any]:
    """Returns trial count K, registered strategies, recent jobs, and worker state."""
    registry = StrategyRegistry()
    return {
        "trial_count_k": registry.trial_count_k,
        "strategies_count": len(registry.get_entries()),
        "strategies": [asdict(e) for e in registry.get_entries()],
        "jobs": [job_summary_dict(j) for j in list_jobs()[:30]],  # no per-coin lists (size)
        "worker": get_worker_status(),
    }


@router.post("/recompute-k")
def recompute_k_endpoint() -> dict[str, Any]:
    """Recompute K from unique (file hash, timeframe) pairs across all jobs."""
    registry = StrategyRegistry()
    k = registry.recompute_k()
    return {"trial_count_k": k}


@router.post("/upload", response_model=UploadResponse)
async def upload_strategy(
    file: UploadFile = File(...),
    auto_start: bool = True,
    timeframes: str | None = Form(None),
    eval_mode: str = Form("standard"),
) -> UploadResponse:
    """Upload a single .py strategy file.

    Validates AST (restricted imports, Strategy protocol),
    saves to uploads/<name>_<version>_<sha256[:8]>.py,
    records in registry, and queues multi-timeframe jobs.
    """
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(400, "Only standalone Python (.py) strategy files are supported.")

    code_bytes = await file.read()
    registry = StrategyRegistry()
    try:
        entry, is_new = registry.register_code(code_bytes, filename_hint=file.filename)
    except StrategyValidationError as e:
        raise HTTPException(422, f"Strategy validation rejected: {e}") from e
    except Exception as e:
        raise HTTPException(400, f"Failed to parse uploaded strategy: {e}") from e

    # Parse timeframes if provided
    selected_tfs = None
    if timeframes:
        tfs = [t.strip() for t in timeframes.split(",") if t.strip()]
        if tfs:
            selected_tfs = tfs

    jobs: list[StrategyLabJob] = []
    if auto_start:
        jobs = start_pipeline_jobs(
            entry,
            timeframes=selected_tfs,
            rules_version="v1",
            auto_start=True,
            eval_mode=eval_mode,
        )

    return UploadResponse(
        strategy_id=entry.strategy_id,
        name=entry.name,
        version=entry.version,
        timeframe=entry.timeframe,
        sha256=entry.sha256,
        short_hash=entry.short_hash,
        trial_k=registry.trial_count_k,
        file_path=entry.file_path,
        uploaded_at=entry.uploaded_at,
        is_new_upload=is_new,
        job_id=jobs[0].job_id if jobs else None,
        job_ids=[j.job_id for j in jobs],
    )


@router.post("/jobs")
def create_pipeline_job(req: JobStartRequest) -> dict[str, Any]:
    """Manually trigger pipeline jobs for a registered strategy."""
    registry = StrategyRegistry()
    entry = registry.get_by_id(req.strategy_id)
    if entry is None:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not found in registry.")

    jobs = start_pipeline_jobs(
        entry,
        timeframes=req.timeframes,
        rules_version=req.rules_version,
        auto_start=True,
        eval_mode=req.eval_mode,
    )
    return {
        "strategy_id": entry.strategy_id,
        "jobs": [job_to_dict(j) for j in jobs],
        "primary_job": job_to_dict(jobs[0]) if jobs else {},
    }


@router.get("/jobs")
def get_jobs_list() -> list[dict[str, Any]]:
    return [job_to_dict(j) for j in list_jobs()]


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    return job_to_dict(job)


@router.get("/jobs/{job_id}/log")
def get_job_log(job_id: str, lines: int = 50) -> dict[str, Any]:
    """Get the last N log lines for a specific job."""
    log_lines = get_job_log_lines(job_id, max_lines=lines)
    return {"job_id": job_id, "lines": log_lines}


@router.post("/jobs/{job_id}/retry")
def retry_pipeline_job(job_id: str) -> dict[str, Any]:
    """Retry a failed, cancelled, or stalled job."""
    try:
        job = retry_job(job_id)
        return {"status": "RETRY_QUEUED", "job": job_to_dict(job)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/jobs/{job_id}/cancel")
def cancel_pipeline_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued or running job."""
    try:
        job = cancel_job(job_id)
        return {"status": "CANCELLED", "job": job_to_dict(job)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


LIBRARY_DIR = Path(__file__).parent / "strategies"


def _library_files() -> dict[str, Path]:
    """Built-in strategy files (strategy_lab/strategies/*.py), keyed by file stem."""
    return {p.stem: p for p in sorted(LIBRARY_DIR.glob("*.py")) if not p.name.startswith("_")}


@router.get("/library")
def get_strategy_library() -> list[dict[str, Any]]:
    """Built-in strategies that can be loaded into the lab without uploading a file.

    Description and params come from each file itself (module docstring, Strategy.params).
    """
    out: list[dict[str, Any]] = []
    for stem, path in _library_files().items():
        try:
            strat = load_strategy(path)
        except Exception as e:  # a broken file is listed with its error, never hidden
            out.append({"file": stem, "error": str(e)})
            continue
        doc = (ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or "").strip()
        title, _, body = doc.partition("\n")
        out.append(
            {
                "file": stem,
                "name": strat.name,
                "version": strat.version,
                "timeframe": strat.timeframe,
                "title": title.strip(),
                "description": " ".join(body.split()),
                "params": dict(strat.params),
            }
        )
    return out


@router.post("/library/{file}/register", response_model=UploadResponse)
def register_library_strategy(file: str) -> UploadResponse:
    """Register a built-in strategy exactly like an uploaded file (no run starts)."""
    path = _library_files().get(file)
    if path is None:  # only names from the listing; no paths
        raise HTTPException(404, f"No built-in strategy '{file}'.")
    registry = StrategyRegistry()
    try:
        entry, is_new = registry.register_code(path.read_bytes(), filename_hint=path.name)
    except StrategyValidationError as e:
        raise HTTPException(422, f"Strategy validation rejected: {e}") from e
    return UploadResponse(
        strategy_id=entry.strategy_id,
        name=entry.name,
        version=entry.version,
        timeframe=entry.timeframe,
        sha256=entry.sha256,
        short_hash=entry.short_hash,
        trial_k=registry.trial_count_k,
        file_path=entry.file_path,
        uploaded_at=entry.uploaded_at,
        is_new_upload=is_new,
    )


@router.post("/clear")
def clear_strategy_lab() -> dict[str, Any]:
    """Delete all jobs, results and logs and empty the strategy list. Trial count K is kept."""
    removed = clear_all_jobs()
    return {"removed_jobs": removed, "trial_count_k": StrategyRegistry().trial_count_k}


@router.get("/comparison-report")
def get_comparison_report_endpoint(strategy_id: str | None = Query(None)) -> dict[str, Any]:
    """Return comparison report rows ordered by 4h -> 2h -> 1h -> 30m -> 15m -> 5m."""
    rows = get_comparison_report(strategy_id)
    return {"report": rows}


@router.get("/comparison-report.csv")
def download_comparison_report_csv_endpoint(strategy_id: str | None = Query(None)) -> Any:
    """Download comparison report as CSV."""
    csv_text = export_comparison_report_csv(strategy_id)
    return StreamingResponse(
        io.BytesIO(csv_text.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="comparison_report.csv"'},
    )


@router.get("/jobs/{job_id}/download/{fmt}")
def download_job_report(job_id: str, fmt: str) -> Any:
    """Download full report as CSV or JSON."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")

    if fmt.lower() == "json":
        data = asdict(job)
        content = json.dumps(data, indent=2).encode("utf-8")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=report_{job_id}.json"},
        )
    elif fmt.lower() == "csv":
        df = pd.DataFrame(job.per_coin_results)
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        return StreamingResponse(
            io.BytesIO(csv_bytes),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=report_{job_id}.csv"},
        )
    else:
        raise HTTPException(400, "Supported formats: csv, json")


@router.post("/jobs/{job_id}/test-2026")
def run_2026_test(job_id: str, req: Test2026Request) -> dict[str, Any]:
    """Execute locked 2026 test split."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")

    if job.verdict != "PASS":
        raise HTTPException(
            403,
            f"2026 TEST locked. Strategy verdict is '{job.verdict}'. Only PASS strategies may unlock 2026 data.",
        )

    if not req.confirm:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "message": (
                "2026 TEST is a single-use out-of-sample verification gate. "
                "Running this will permanently record 2026 performance and consume the out-of-sample period. "
                "Send {'confirm': true} to proceed."
            ),
        }

    job.test_2026_status = "RUNNING"
    return {
        "status": "INITIATED",
        "message": "2026 test execution initiated (data download & scan queued).",
        "job_id": job_id,
    }
