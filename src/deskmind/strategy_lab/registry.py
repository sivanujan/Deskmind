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
    """Persistent registry for uploaded strategy files with trial count K tracking."""

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
        self._load()

    def _load(self) -> None:
        if self.registry_file.exists():
            try:
                data = json.loads(self.registry_file.read_text(encoding="utf-8"))
                self._entries = [StrategyRegistryEntry(**item) for item in data.get("entries", [])]
                self._trial_counter = int(data.get("trial_count_k", len(self._entries)))
            except Exception:
                self._entries = []
                self._trial_counter = 0
        else:
            self._entries = []
            self._trial_counter = 0

    def _save(self) -> None:
        payload = {
            "trial_count_k": self.trial_count_k,
            "updated_at": datetime.now(UTC).isoformat(),
            "entries": [asdict(e) for e in self._entries],
        }
        self.registry_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @property
    def trial_count_k(self) -> int:
        """Trial count K represents the number of trials registered/executed."""
        unique_hashes = {e.sha256 for e in self._entries}
        return max(getattr(self, "_trial_counter", len(unique_hashes)), len(unique_hashes))

    def allocate_trial_k(self) -> int:
        """Allocate and return next trial K for a job."""
        self._trial_counter = self.trial_count_k + 1
        self._save()
        return self._trial_counter

    def get_entries(self) -> list[StrategyRegistryEntry]:
        return list(self._entries)

    def get_by_id(self, strategy_id: str) -> StrategyRegistryEntry | None:
        for e in self._entries:
            if e.strategy_id == strategy_id or e.short_hash == strategy_id:
                return e
        return None

    def register_code(
        self,
        code_bytes: bytes,
        filename_hint: str | None = None,
    ) -> tuple[StrategyRegistryEntry, bool]:
        """Validate AST, save to uploads, and register with trial count K.

        Returns:
            (entry, is_new): is_new is True if sha256 was not previously seen.
        """
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

        # Increment trial count K
        next_k = self.trial_count_k + 1
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        entry = StrategyRegistryEntry(
            strategy_id=f"{safe_name}_{short_hash}",
            name=name,
            version=version,
            timeframe=timeframe,
            sha256=sha,
            short_hash=short_hash,
            trial_k=next_k,
            file_path=str(target_path).replace("\\", "/"),
            uploaded_at=now_str,
            params=params,
        )

        self._entries.append(entry)
        self._save()
        return entry, True
