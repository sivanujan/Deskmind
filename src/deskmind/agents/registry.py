"""Agent registry: the single source of truth for which agents exist.

The dashboard lists exactly these agents, with versions taken from each agent's versioned config.
Agents on the roadmap that are not built yet are listed separately as `PLANNED` so the UI can
say "not built yet" instead of inventing a status.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from deskmind.agents.trend.config import AGENT_NAME as TREND_NAME
from deskmind.agents.trend.config import AGENT_VERSION as TREND_VERSION
from deskmind.agents.trend.config import DEFAULT_CONFIG as TREND_CONFIG


@dataclass(frozen=True)
class AgentInfo:
    name: str
    version: str
    kind: str  # "deterministic" | "llm"
    spec: str  # path of the rule spec
    summary: str
    params: dict


@dataclass(frozen=True)
class PlannedAgent:
    name: str
    roadmap_step: int
    summary: str


AGENTS: tuple[AgentInfo, ...] = (
    AgentInfo(
        name=TREND_NAME,
        version=TREND_VERSION,
        kind="deterministic",
        spec="docs/agents/trend.md",
        summary=(
            "Swings (HH/HL/LH/LL), BOS/CHoCH, sweeps and trend lines per timeframe; no look-ahead."
        ),
        params=TREND_CONFIG.as_dict(),
    ),
)

PLANNED: tuple[PlannedAgent, ...] = (
    PlannedAgent(
        name="head",
        roadmap_step=11,
        summary=(
            "LLM head agent (OpenRouter) + Telegram advisories: explains and proposes from the "
            "analyst reports, never sizes. Not built yet."
        ),
    ),
)


def spec_intro(agent: AgentInfo, root: Path = Path(".")) -> str | None:
    """First paragraph after the title of the agent's spec, if the spec exists."""
    path = root / agent.spec
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    para: list[str] = []
    for line in lines[1:]:
        if line.strip():
            para.append(line.strip())
        elif para:
            break
    return " ".join(para) or None
