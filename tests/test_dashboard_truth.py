"""The dashboard shows only real data.

1. Static check of apps/web/src: no hardcoded risk limits, agent names/versions, strategy names or
   parameters, and no mock fallbacks. Every value must come from a backend endpoint.
2. Every API path the frontend calls exists in the FastAPI app.
3. Contract tests: the truth endpoints equal their sources (RiskConfig, agent registry, strategy
   classes, split config).
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from deskmind.agents.registry import AGENTS, PLANNED
from deskmind.agents.trend.config import DEFAULT_CONFIG as TREND_CONFIG
from deskmind.api.app import create_app
from deskmind.backtest.runner import STRATEGY_REGISTRY
from deskmind.config import BACKTEST_SPLITS, Settings
from deskmind.risk import config as rc

ROOT = Path(__file__).resolve().parents[1]
WEB_SRC = ROOT / "apps" / "web" / "src"


def web_files() -> list[Path]:
    return sorted(p for p in WEB_SRC.rglob("*") if p.suffix in (".ts", ".tsx"))


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", text)


# ---------------------------------------------------------------- 1. static check


def _forbidden_patterns() -> list[tuple[str, re.Pattern[str]]]:
    pats: list[tuple[str, re.Pattern[str]]] = []

    def lit(label: str, s: str) -> None:
        pats.append((label, re.compile(re.escape(s), re.I)))

    # Agent and strategy identity must come from /api/dashboard/{agents,strategies}.
    for a in AGENTS:
        lit(f"agent version {a.name}", f'"{a.version}"')
        lit(f"agent version {a.name}", f"v{a.version}")
    for p in PLANNED:
        lit(f"planned agent {p.name}", f"{p.name} agent")
    for name, cls in STRATEGY_REGISTRY.items():
        lit("strategy name", name)
        lit(f"strategy version {name}", f"v{cls.version}")
    pats.append(("semver literal", re.compile(r"[\"'`]v?\d+\.\d+\.\d+[\"'`]")))

    # Parameters/limits assigned a literal value (`max_leverage: 5`, `swing = 3`, `?? 50000`).
    names = [f.name for f in dataclasses.fields(rc.RiskConfig)]
    names += [f.name for f in dataclasses.fields(type(TREND_CONFIG))]
    for cls in STRATEGY_REGISTRY.values():
        names += list(cls.default_params)
    # names the old dashboard invented
    names += ["max_risk_pct", "max_notional_mult", "max_open_trades", "max_position_notional"]
    names += ["atr_stop_multiplier", "swing_lookback", "risk_fraction", "risk_reward_ratio"]
    for n in sorted(set(names)):
        pats.append((f"hardcoded value for {n}", re.compile(rf"\b{n}\b\s*[:=]\s*[-\d.\"']")))

    # Invented rules and figures that were on the dashboard before.
    for s in ("Halt & Close", "halt_and_close", "trailing stop", "trendline bounce", "$50,000",
              "50000", "50_000", "Head Agent", "latency_ms", "edge_status"):  # fmt: skip
        lit("invented content", s)

    # Mock fallbacks: a catch handler that fills state with data instead of showing the error.
    pats.append(
        (
            "mock fallback in catch",
            re.compile(
                r"catch\(\s*(?:\(\s*\w*\s*\)|\w+)\s*=>\s*\{?\s*"
                r"(?:if\s*\([^)]*\)\s*\{?\s*)?set\w+\(\s*[\[{]"
            ),
        )
    )
    pats.append(("mock data", re.compile(r"\b(mock|dummy|fake)\w*\s*[:=]", re.I)))
    # A backend value replaced by an invented default (`?? "normal"`, `?? 50000`).
    pats.append(
        (
            "invented default",
            re.compile(r'\?\?\s*(?:\d{4,}|"(?:normal|testnet|paper|auto|healthy|synced)")', re.I),
        )
    )
    pats.append(
        (
            "invented initial state",
            re.compile(r'useState(?:<[^>]*>)?\(\s*"(?:normal|testnet|paper|auto|healthy)"', re.I),
        )
    )
    return pats


def test_frontend_has_no_hardcoded_truth() -> None:
    pats = _forbidden_patterns()
    hits: list[str] = []
    for path in web_files():
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for label, pat in pats:  # whole file, so multi-line fallbacks are caught too
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                snippet = " ".join(m.group(0).split())[:120]
                hits.append(f"{path.relative_to(ROOT)}:{line}: {label}: {snippet}")
    assert not hits, "frontend contains hardcoded values:\n" + "\n".join(hits)


def test_static_check_catches_the_old_fakes(tmp_path: Path) -> None:
    """The patterns must flag what the dashboard used to contain."""
    samples = [
        '{ name: "Head Agent (LLM Risk)", version: "0.2.0" }',
        "swing_lookback: 5,",
        "max_open_positions: 3,",
        "const x = risk?.max_notional ?? 50000;",
        '.catch(() => {\n  setAgents([\n    { name: "trend" }]); })',
        'const state = risk?.state ?? "normal";',
        "drawdown_halt_pct: 0.15",
        'const [riskState, setRiskState] = useState<string>("normal");',
    ]
    pats = _forbidden_patterns()
    for s in samples:
        assert any(p.search(s) for _, p in pats), s


# ---------------------------------------------------------------- 2. API paths exist


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(_env_file=None, postgres_password="x", data_dir=tmp_path)
    from deskmind.universe.store import MemoryUniverseStore

    return create_app(
        settings,
        redis=fakeredis.FakeAsyncRedis(decode_responses=True),
        health_checks=False,
        universe_store=MemoryUniverseStore(),
    )


def test_every_frontend_api_path_exists(app) -> None:
    routes = list(app.openapi()["paths"])
    ws = ["/ws/live", "/ws/backtest/{job_id}"]
    patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r) + "$") for r in routes + ws]
    text = (WEB_SRC / "lib" / "api.ts").read_text(encoding="utf-8")
    used = set(re.findall(r"[\"`](/(?:api|ws)/[^\"`?]*)", text))
    assert len(used) > 20
    missing = []
    for path in used:
        concrete = re.sub(r"\$\{[^}]+\}", "x", path)
        variants = [concrete]
        if "${action}" in path:  # decideApproval
            variants = [path.replace("${action}", a) for a in ("approve", "reject")]
        for v in variants:
            v = re.sub(r"\$\{[^}]+\}", "x", v)
            if not any(p.match(v) for p in patterns):
                missing.append(v)
    assert not missing, f"frontend calls endpoints that do not exist: {missing}"


# ---------------------------------------------------------------- 3. contract tests


def test_risk_page_fields_equal_risk_config(app) -> None:
    r = TestClient(app).get("/api/dashboard/risk")
    assert r.status_code == 200
    body = r.json()
    cfg = rc.RiskConfig()
    assert set(body["config"]) == {f.name for f in dataclasses.fields(cfg)}
    for f in dataclasses.fields(cfg):
        v = getattr(cfg, f.name)
        got = body["config"][f.name]
        assert (str(v) if not isinstance(v, bool | int) else v) == got, f.name
    assert body["hard_caps"] == {
        "max_risk_pct": str(rc.HARD_CAP_MAX_RISK_PCT),
        "max_stop_price_pct": str(rc.HARD_CAP_MAX_STOP_PRICE_PCT),
        "max_leverage": rc.HARD_CAP_MAX_LEVERAGE,
        "max_notional_mult": str(rc.HARD_CAP_MAX_NOTIONAL_MULT),
    }
    # the documented rules as they actually are
    assert body["config"]["max_open_positions"] == 1
    assert body["config"]["max_notional_mult"] == "3.0"
    assert body["config"]["drawdown_halt_pct"] == "0.15"
    assert "new entries" in body["notes"]["drawdown_halt_pct"]
    assert body["groups"]["max_stop_price_pct"].startswith("Rule 2")
    # equity per mode: mainnet is never used for sizing
    assert body["equity_by_mode"]["mainnet"]["used_when"] == "never"
    assert set(body["equity_by_mode"]) == {"paper", "testnet", "mainnet"}


def test_risk_unknown_state_is_null_not_invented(app, monkeypatch, tmp_path) -> None:
    import deskmind.api.control as control_mod
    import deskmind.api.dashboard as dash

    monkeypatch.setattr(dash, "TRADE_STATUS_FILE", tmp_path / "missing.json")
    settings = Settings(_env_file=None, postgres_password="x", data_dir=tmp_path)
    monkeypatch.setattr(control_mod, "get_settings", lambda: settings)
    body = TestClient(app).get("/api/dashboard/risk").json()
    assert body["current_equity"]["value"] is None
    assert body["current_equity"]["reason"]
    assert body["breaker"]["state"] is None
    assert body["breaker"]["reason"]


def test_agents_endpoint_lists_only_registry(app) -> None:
    body = TestClient(app).get("/api/dashboard/agents").json()
    assert [(a["name"], a["version"]) for a in body["agents"]] == [
        (a.name, a.version) for a in AGENTS
    ]
    trend = body["agents"][0]
    assert trend["version"] == TREND_CONFIG.version
    assert trend["params"]["left_bars"] == TREND_CONFIG.left_bars
    assert trend["runtime"]["state"] == "never_run"  # empty fakeredis: no heartbeat
    assert trend["runtime"]["errors_24h"] == 0
    assert [p["name"] for p in body["planned"]] == [p.name for p in PLANNED]
    assert all("Not built yet" in p["summary"] for p in body["planned"])


def test_strategies_endpoint_uses_class_docstring_and_params(app) -> None:
    body = TestClient(app).get("/api/dashboard/strategies").json()
    assert [s["name"] for s in body] == list(STRATEGY_REGISTRY)
    for s in body:
        cls = STRATEGY_REGISTRY[s["name"]]
        assert s["version"] == cls.version
        assert s["description"] == (sys.modules[cls.__module__].__doc__ or "").strip()
        assert s["params"] == cls.default_params
        assert s["agent_params"]["trend.left_bars"] == TREND_CONFIG.left_bars
        assert s["agent_params"]["trend.right_bars"] == TREND_CONFIG.right_bars
    tf = next(s for s in body if s["name"] == "trend_follow_v1")
    assert "No trailing" in tf["description"]
    assert tf["timeframes"] == ["1h", "15m"]


def test_backtest_meta_matches_config(app) -> None:
    body = TestClient(app).get("/api/dashboard/backtest-meta").json()
    for name, (start, end) in BACKTEST_SPLITS.items():
        assert body["splits"][name]["start"] == start
        assert body["splits"][name]["end"] == end
    assert body["splits"]["test"]["ui_allowed"] is False
    assert body["strategies"] == list(STRATEGY_REGISTRY)


def test_account_is_read_only_and_never_for_sizing(app, monkeypatch, tmp_path) -> None:
    import deskmind.api.dashboard as dash

    monkeypatch.setattr(dash, "META_DIR", tmp_path)
    body = TestClient(app).get("/api/dashboard/account").json()
    assert body["read_only"] is True
    assert body["used_for_sizing"] is False
    assert body["snapshot"] is None
    assert body["reason"]
    assert "balances" not in body
