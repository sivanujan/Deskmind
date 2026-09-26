"""Run registry and multiple-testing accounting for backtest runs.

Records every run (completed, discarded, or failed) in PostgreSQL (backtest_runs table)
and maintains a local JSON audit trail under reports/backtests/registry.json.
Enforces the locked test split: runs on the test split are strictly refused unless
explicitly unlocked with `--unlock-test` and typed confirmation.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from deskmind.config import BACKTEST_SPLITS
from deskmind.db.models import BacktestRun

logger = logging.getLogger("deskmind.backtest.registry")
REGISTRY_FILE = Path("reports/backtests/registry.json")


def get_git_commit() -> str | None:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode("utf-8").strip()
    except Exception:
        return None


def validate_split_access(
    split: str, start: datetime, end: datetime, unlock_test: bool, confirm_text: str | None = None
) -> None:
    """Refuse runs on the locked TEST split unless explicitly unlocked."""
    test_start = datetime.fromisoformat(BACKTEST_SPLITS["test"][0]).replace(tzinfo=UTC)

    # Check if split is "test" or if date range reaches into test period
    is_test_split = (split.lower() == "test") or (end > test_start)

    if is_test_split and not unlock_test:
        test_from = BACKTEST_SPLITS["test"][0]
        raise PermissionError(
            f"REFUSAL: The TEST split ({test_from} to latest) is LOCKED to prevent p-hacking "
            "and out-of-sample data contamination. "
            "To run on the test split, you must explicitly pass --unlock-test and "
            "provide confirmation."
        )

    if is_test_split and unlock_test:
        if confirm_text != "CONFIRM":
            raise PermissionError(
                "REFUSAL: Test split unlock requires confirmation text 'CONFIRM'. "
                "Every run on the test set is permanently logged."
            )
        logger.warning(
            "AUDIT: TEST SPLIT UNLOCKED by user. This run is permanently recorded in the registry."
        )


def save_run_record_local(record: dict[str, Any], registry_file: Path = REGISTRY_FILE) -> None:
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    if registry_file.exists():
        try:
            with open(registry_file, encoding="utf-8") as f:
                runs = json.load(f)
        except Exception:
            runs = []
    runs.append(record)
    with open(registry_file, "w", encoding="utf-8") as f:
        json.dump(runs, f, indent=2)


async def save_run_record_db(engine: AsyncEngine, record: dict[str, Any]) -> None:
    from deskmind.db.session import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        run_obj = BacktestRun(
            run_id=record["run_id"],
            strategy_name=record["strategy_name"],
            strategy_version=record["strategy_version"],
            params=record["params"],
            agent_versions=record["agent_versions"],
            symbols=record["symbols"],
            split=record["split"],
            start_date=record["start_date"],
            end_date=record["end_date"],
            engine_version=record["engine_version"],
            git_commit=record.get("git_commit"),
            metrics=record["metrics"],
            notes=record.get("notes"),
            status=record.get("status", "completed"),
        )
        session.add(run_obj)
        await session.commit()


def list_runs_local(registry_file: Path = REGISTRY_FILE) -> list[dict[str, Any]]:
    if not registry_file.exists():
        return []
    try:
        with open(registry_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


async def list_runs_db(engine: AsyncEngine) -> list[dict[str, Any]]:
    from deskmind.db.session import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        result = await session.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()))
        rows = list(result.scalars())
        return [
            {
                "run_id": r.run_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "strategy_name": r.strategy_name,
                "strategy_version": r.strategy_version,
                "split": r.split,
                "symbols": r.symbols,
                "engine_version": r.engine_version,
                "metrics": r.metrics,
                "status": r.status,
            }
            for r in rows
        ]
