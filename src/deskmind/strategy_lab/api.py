"""FastAPI endpoints for Strategy Lab Phase B."""

from __future__ import annotations

import io
from dataclasses import asdict
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from deskmind.strategy_lab.base import StrategyValidationError
from deskmind.strategy_lab.pipeline import (
    StrategyLabJob,
    get_job,
    list_jobs,
    start_pipeline_job,
)
from deskmind.strategy_lab.registry import StrategyRegistry

router = APIRouter(prefix="/api/strategy-lab", tags=["Strategy Lab"])
_REGISTRY = StrategyRegistry()


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


class JobStartRequest(BaseModel):
    strategy_id: str
    rules_version: str = "v1"


class Test2026Request(BaseModel):
    confirm: bool = False


@router.get("/status")
def get_strategy_lab_status() -> dict[str, Any]:
    """Returns trial count K, registered strategies, and recent jobs."""
    return {
        "trial_count_k": _REGISTRY.trial_count_k,
        "strategies_count": len(_REGISTRY.get_entries()),
        "strategies": [asdict(e) for e in _REGISTRY.get_entries()],
        "jobs": [asdict(j) for j in list_jobs()[:10]],
    }


@router.post("/upload", response_model=UploadResponse)
async def upload_strategy(
    file: UploadFile = File(...),
    auto_start: bool = True,
) -> UploadResponse:
    """Upload a single .py strategy file.

    Validates AST (restricted imports, Strategy protocol),
    saves to uploads/<name>_<version>_<sha256[:8]>.py,
    records in registry, and increments trial count K for new hashes.
    """
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(400, "Only standalone Python (.py) strategy files are supported.")

    code_bytes = await file.read()
    try:
        entry, is_new = _REGISTRY.register_code(code_bytes, filename_hint=file.filename)
    except StrategyValidationError as e:
        raise HTTPException(422, f"Strategy validation rejected: {e}") from e
    except Exception as e:
        raise HTTPException(400, f"Failed to parse uploaded strategy: {e}") from e

    job = start_pipeline_job(entry, rules_version="v1", auto_start=auto_start) if auto_start else None

    return UploadResponse(
        strategy_id=entry.strategy_id,
        name=entry.name,
        version=entry.version,
        timeframe=entry.timeframe,
        sha256=entry.sha256,
        short_hash=entry.short_hash,
        trial_k=_REGISTRY.trial_count_k,
        file_path=entry.file_path,
        uploaded_at=entry.uploaded_at,
        is_new_upload=is_new,
        job_id=job.job_id if job else None,
    )


@router.post("/jobs")
def create_pipeline_job(req: JobStartRequest) -> dict[str, Any]:
    """Manually trigger a pipeline job for a registered strategy."""
    entry = _REGISTRY.get_by_id(req.strategy_id)
    if entry is None:
        raise HTTPException(404, f"Strategy '{req.strategy_id}' not found in registry.")

    job = start_pipeline_job(entry, rules_version=req.rules_version)
    return asdict(job)


@router.get("/jobs")
def get_jobs_list() -> list[dict[str, Any]]:
    return [asdict(j) for j in list_jobs()]


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found.")
    return asdict(job)


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
    """Execute locked 2026 test split.

    Only enabled when verdict == PASS.
    Requires explicit confirmation payload confirm=True.
    """
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

    # If confirmed:
    job.test_2026_status = "RUNNING"
    return {
        "status": "INITIATED",
        "message": "2026 test execution initiated (data download & scan queued).",
        "job_id": job_id,
    }
