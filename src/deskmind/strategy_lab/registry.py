"""Strategy Lab registry managing uploaded strategies and trial count K."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deskmind.strategy_lab.base import load_strategy, validate_strategy_ast


@dataclass
class StrategyRegistryEntry:
    strategy_id: str
    name: str
    version: str
    timeframe: str
    sha256: str
    short_hash: str
    trial_k: int
    file_path: str
    uploaded_at: str
    params: dict[str, Any]


class StrategyRegistry:
    """Persistent registry for uploaded strategy files with trial count K tracking.

    K = number of UNIQUE (file hash, timeframe) pairs ever run.
    Re-running the same file hash on the same timeframe does NOT increase K.
    """

    def __init__(
        self,
        registry_file: Path | str = "data/strategy_lab/registry.json",
        uploads_dir: Path | str = "uploads",
    ) -> None:
        self.registry_file = Path(registry_file)
        self.uploads_dir = Path(uploads_dir)
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[StrategyRegistryEntry] = []
        self._executed_runs: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.registry_file.exists():
            try:
                data = json.loads(self.registry_file.read_text(encoding="utf-8"))
                self._entries = [StrategyRegistryEntry(**item) for item in data.get("entries", [])]
                self._executed_runs = data.get("executed_runs", [])
                # If executed_runs is empty but trial_count_k exists, we will reconstruct
                if not self._executed_runs and "trial_count_k" in data and self._entries:
                    for idx, e in enumerate(self._entries, 1):
                        self._executed_runs.append({
                            "sha256": e.sha256,
                            "timeframe": e.timeframe or "4h",
                            "trial_k": idx,
                            "strategy_name": e.name,
                            "recorded_at": e.uploaded_at,
                        })
            except Exception:
                self._entries = []
                self._executed_runs = []
        else:
            self._entries = []
            self._executed_runs = []

    def _save(self) -> None:
        payload = {
            "trial_count_k": self.trial_count_k,
            "updated_at": datetime.now(UTC).isoformat(),
            "entries": [asdict(e) for e in self._entries],
            "executed_runs": self._executed_runs,
        }
        self.registry_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @property
    def trial_count_k(self) -> int:
        """Trial count K represents the number of UNIQUE (file hash, timeframe) pairs."""
        return len(self._executed_runs)

    def get_trial_k(self) -> int:
        """Return the current trial count K."""
        return self.trial_count_k

    def allocate_trial_k(
        self,
        sha256: str | None = None,
        timeframe: str | None = None,
        strategy_name: str = "",
        mode: str = "standard",
    ) -> int:
        """Allocate and return trial K for a (sha256, timeframe, mode) run.

        Re-running the same (file hash, timeframe, mode) returns the existing trial_k
        and does NOT increase K.
        """
        self._load()
        tf = timeframe or "4h"
        eval_mode = mode or "standard"
        if sha256:
            for r in self._executed_runs:
                if (
                    r.get("sha256") == sha256
                    and r.get("timeframe") == tf
                    and r.get("mode", "standard") == eval_mode
                ):
                    return int(r["trial_k"])

            next_k = len(self._executed_runs) + 1
            now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._executed_runs.append({
                "sha256": sha256,
                "timeframe": tf,
                "mode": eval_mode,
                "trial_k": next_k,
                "strategy_name": strategy_name,
                "recorded_at": now_str,
            })
            self._save()
            return next_k
        else:
            next_k = len(self._executed_runs) + 1
            self._executed_runs.append({
                "sha256": "anonymous",
                "timeframe": tf,
                "mode": eval_mode,
                "trial_k": next_k,
                "strategy_name": strategy_name,
                "recorded_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            })
            self._save()
            return next_k

    def recompute_k(self, jobs_dir: Path | str = "data/strategy_lab/jobs") -> int:
        """Recompute K from unique (file hash, timeframe, mode) pairs ever run.

        Scans all jobs in jobs_dir, orders unique pairs chronologically,
        assigns sequential trial_k (1, 2, ...), updates registry.json,
        and updates job.json files.
        """
        self._load()
        j_dir = Path(jobs_dir)
        strat_map: dict[str, StrategyRegistryEntry] = {}
        for e in self._entries:
            strat_map[e.strategy_id] = e
            strat_map[e.short_hash] = e

        all_jobs: list[tuple[datetime, dict[str, Any], Path]] = []
        if j_dir.exists():
            for sub in j_dir.iterdir():
                jf = sub / "job.json"
                if sub.is_dir() and jf.exists():
                    try:
                        jd = json.loads(jf.read_text(encoding="utf-8"))
                        dt_str = jd.get("created_at", "")
                        try:
                            dt = datetime.strptime(dt_str.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            dt = datetime.min
                        all_jobs.append((dt, jd, jf))
                    except Exception:
                        pass

        all_jobs.sort(key=lambda x: x[0])

        def _ts(text: str) -> datetime:
            try:
                return datetime.strptime(text.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                return datetime.min

        # Every (sha, tf, mode) ever tried, ordered by its first job
        first_seen: dict[tuple[str, str, str], tuple[datetime, dict[str, Any]]] = {}
        job_pairs: list[tuple[tuple[str, str, str], dict[str, Any], Path]] = []
        for dt, jd, jf in all_jobs:
            s_id = jd.get("strategy_id", "")
            tf = jd.get("timeframe") or "4h"
            mode = jd.get("eval_mode") or jd.get("mode") or "standard"
            strat_entry = strat_map.get(s_id)
            sha = strat_entry.sha256 if strat_entry else (jd.get("strategy_path") or s_id)
            pair = (sha, tf, mode)
            job_pairs.append((pair, jd, jf))
            if pair not in first_seen or dt < first_seen[pair][0]:
                first_seen[pair] = (
                    dt,
                    {
                        "sha256": sha,
                        "timeframe": tf,
                        "mode": mode,
                        "strategy_name": jd.get("strategy_name", ""),
                        "recorded_at": jd.get("created_at", ""),
                    },
                )

        for r in self._executed_runs:
            pair = (r.get("sha256", ""), r.get("timeframe") or "4h", r.get("mode", "standard"))
            if pair not in first_seen:
                first_seen[pair] = (_ts(r.get("recorded_at", "")), dict(r))

        new_runs: list[dict[str, Any]] = []
        pair_to_k: dict[tuple[str, str, str], int] = {}
        for pair, (_, run) in sorted(first_seen.items(), key=lambda kv: kv[1][0]):
            pair_to_k[pair] = len(pair_to_k) + 1
            new_runs.append({**run, "trial_k": pair_to_k[pair]})

        for pair, jd, jf in job_pairs:
            k_val = pair_to_k[pair]
            if jd.get("trial_k") != k_val:  # update job file with correct trial_k
                jd["trial_k"] = k_val
                try:
                    jf.write_text(json.dumps(jd, indent=2), encoding="utf-8")
                except Exception:
                    pass
        self._executed_runs = new_runs
        self._save()
        return len(self._executed_runs)

    def clear_entries(self) -> None:
        """Forget the uploaded strategies (the list). Trial count K is kept on purpose."""
        self._load()
        self._entries = []
        self._save()

    def get_entries(self) -> list[StrategyRegistryEntry]:
        self._load()
        return list(self._entries)

    def get_by_id(self, strategy_id: str) -> StrategyRegistryEntry | None:
        self._load()
        for e in self._entries:
            if e.strategy_id == strategy_id or e.short_hash == strategy_id:
                return e
        return None

    def register_code(
        self,
        code_bytes: bytes,
        filename_hint: str | None = None,
    ) -> tuple[StrategyRegistryEntry, bool]:
        """Validate AST, save to uploads, and register in registry.

        Returns:
            (entry, is_new): is_new is True if sha256 was not previously seen.
        """
        self._load()
        source_code = code_bytes.decode("utf-8")
        # 1. AST inspection (raises StrategyValidationError if invalid)
        validate_strategy_ast(source_code)

        sha = hashlib.sha256(code_bytes).hexdigest()
        short_hash = sha[:8]

        # 2. Check if already registered
        for existing in self._entries:
            if existing.sha256 == sha:
                # Same file re-uploaded: does NOT increment K
                return existing, False

        # 3. Save temporarily to load metadata
        temp_dir = self.uploads_dir / "_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"strat_{short_hash}.py"
        temp_path.write_bytes(code_bytes)

        try:
            strat = load_strategy(temp_path)
            name = getattr(strat, "name", "UnknownStrategy")
            version = getattr(strat, "version", "1.0.0")
            timeframe = getattr(strat, "timeframe", "4h")
            params = getattr(strat, "params", {})
        finally:
            if temp_path.exists():
                temp_path.unlink()

        # Sanitize filename components
        safe_name = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
        safe_version = "".join(c for c in version if c.isalnum() or c in (".", "_", "-"))
        target_filename = f"{safe_name}_{safe_version}_{short_hash}.py"
        target_path = self.uploads_dir / target_filename
        target_path.write_bytes(code_bytes)

        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        k_val = self.allocate_trial_k(sha, timeframe, name, mode="standard")

        entry = StrategyRegistryEntry(
            strategy_id=f"{safe_name}_{short_hash}",
            name=name,
            version=version,
            timeframe=timeframe,
            sha256=sha,
            short_hash=short_hash,
            trial_k=k_val,
            file_path=str(target_path).replace("\\", "/"),
            uploaded_at=now_str,
            params=params,
        )

        self._entries.append(entry)
        self._save()
        return entry, True
