"""Dashboard truth endpoints: every value comes from the real source of truth.

- /api/dashboard/agents      agent registry + Redis health + agent_reports in Postgres
- /api/dashboard/strategies  strategy classes (docstrings, declared params) + backtest registry
- /api/dashboard/risk        RiskConfig + hard caps + breaker state/equity from the trade service
- /api/dashboard/account     last read-only mainnet sync snapshot (never used for sizing)

Anything that is not available is returned as null with a `reason`, never as a made-up value.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from deskmind.agents.registry import AGENTS, PLANNED, spec_intro
from deskmind.api.auth import SessionData, get_current_session, require_csrf
from deskmind.config import DEFAULT_INITIAL_CASH, get_settings
from deskmind.logging import get_logger

log = get_logger(__name__)
router = APIRouter()

TRADE_STATUS_FILE = Path("data/trade/trade_status.json")
TRADE_STATUS_FRESH_S = 60
META_DIR = Path("data/meta")


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, tuple):
        return list(v)
    return v


def read_trade_status(path: Path = TRADE_STATUS_FILE) -> tuple[dict[str, Any] | None, float | None]:
    """(status written by the trade service, age in seconds) or (None, None)."""
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    return data, round(time.time() - path.stat().st_mtime, 1)


# ---------------------------------------------------------------- agents


@router.get("/api/dashboard/agents")
async def dashboard_agents(request: Request) -> dict[str, Any]:
    from deskmind.agents.trend.runner import ERRORS_KEY, HEALTH_KEY
    from deskmind.data.live import buffers as B

    redis = getattr(request.app.state, "redis", None)
    now = B.now_ms()
    out = []
    for agent in AGENTS:
        runtime: dict[str, Any] = {"state": "unknown", "reason": None}
        if agent.name == "trend" and redis is not None:
            try:
                h = await redis.hgetall(HEALTH_KEY)
                hb = int(h.get("heartbeat", 0) or 0)
                runtime.update(
                    heartbeat_age_s=round((now - hb) / 1000, 1) if hb else None,
                    last_report_as_of=h.get("last_report"),
                    reports_since_start=int(h["reports"]) if h.get("reports") else None,
                    last_error=h.get("last_error"),
                    last_error_at=int(h["last_error_at"]) if h.get("last_error_at") else None,
                    errors_24h=int(await redis.zcount(ERRORS_KEY, now - 86_400_000, now)),
                )
                if not hb:
                    runtime.update(state="never_run", reason="no heartbeat recorded")
                elif h.get("state") == "stopped":
                    runtime.update(state="stopped", reason="agent reported a clean shutdown")
                elif now - hb > 30_000:
                    runtime.update(state="stopped", reason="no heartbeat in the last 30 s")
                else:
                    runtime["state"] = "running"
            except Exception as exc:
                runtime.update(state="unknown", reason=f"redis unavailable: {exc}")
        runtime.update(await _agent_report_stats(agent.name, agent.version))
        out.append(
            {
                "name": agent.name,
                "version": agent.version,
                "kind": agent.kind,
                "spec": agent.spec,
                "summary": agent.summary,
                "spec_intro": spec_intro(agent),
                "params": {k: _jsonable(v) for k, v in agent.params.items()},
                "runtime": runtime,
            }
        )
    return {
        "agents": out,
        "planned": [dataclasses.asdict(p) for p in PLANNED],
    }


async def _agent_report_stats(name: str, version: str) -> dict[str, Any]:
    from deskmind.db.models import AgentReport
    from deskmind.db.session import session_scope

    try:
        async with session_scope() as s:
            last = (
                await s.execute(
                    select(AgentReport.as_of, AgentReport.created_at)
                    .where(AgentReport.agent_name == name, AgentReport.version == version)
                    .order_by(AgentReport.created_at.desc())
                    .limit(1)
                )
            ).first()
            count_24h = (
                await s.execute(
                    select(func.count())
                    .select_from(AgentReport)
                    .where(
                        AgentReport.agent_name == name,
                        AgentReport.version == version,
                        AgentReport.created_at >= func.now() - func.make_interval(0, 0, 0, 1),
                    )
                )
            ).scalar_one()
        return {
            "last_report_stored_at": last.created_at.isoformat() if last else None,
            "last_report_stored_as_of": last.as_of.isoformat() if last and last.as_of else None,
            "reports_stored_24h": count_24h,
        }
    except Exception as exc:
        return {"reports_error": f"database unavailable: {exc}"}


# ---------------------------------------------------------------- strategies


@router.get("/api/dashboard/strategies")
async def dashboard_strategies() -> list[dict[str, Any]]:
    from deskmind.backtest.registry import list_runs_local
    from deskmind.backtest.runner import STRATEGY_REGISTRY

    runs = list_runs_local()
    out = []
    for key, cls in STRATEGY_REGISTRY.items():
        module_doc = inspect.getdoc(sys.modules[cls.__module__]) or ""
        mine = [r for r in runs if r.get("strategy_name") == key]
        by_split: dict[str, list[dict[str, Any]]] = {}
        for r in mine:
            by_split.setdefault(r.get("split", "?"), []).append(r)
        latest = {}
        for split, rs in by_split.items():
            r = max(rs, key=lambda x: x.get("created_at", ""))
            m = r.get("metrics") or {}
            latest[split] = {
                "run_id": r.get("run_id"),
                "created_at": r.get("created_at"),
                "strategy_version": r.get("strategy_version"),
                "total_trades": m.get("total_trades"),
                "expectancy_r": m.get("expectancy_r"),
                "profit_factor": m.get("profit_factor"),
                "net_return_pct": m.get("net_return_pct"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),
            }
        agent_params: dict[str, Any] = {}
        if "trend" in cls.uses_agents:
            from deskmind.agents.trend.config import DEFAULT_CONFIG

            agent_params = {
                f"trend.{k}": _jsonable(v)
                for k, v in DEFAULT_CONFIG.as_dict().items()
                if k in ("left_bars", "right_bars", "atr_period", "min_swing_atr", "version")
            }
        if "validation" in latest:
            validation = "validation runs exist (see metrics)"
        elif mine:
            validation = "not validated: train-split runs only"
        else:
            validation = "not tested: no backtest runs"
        out.append(
            {
                "name": key,
                "version": cls.version,
                "description": module_doc,
                "params": {k: _jsonable(v) for k, v in cls.default_params.items()},
                "agent_params": agent_params,
                "uses_agents": list(cls.uses_agents),
                "timeframes": list(cls.timeframes),
                "runs_total": len(mine),
                "runs_by_split": {k: len(v) for k, v in by_split.items()},
                "latest_by_split": latest,
                "validation_status": validation,
            }
        )
    return out


# ---------------------------------------------------------------- risk


def risk_field_notes() -> tuple[dict[str, str], dict[str, str]]:
    """Per-field comments and section headers, read from risk/config.py's own source.

    The dashboard shows these instead of writing its own descriptions of the rules.
    """
    import re

    from deskmind.risk import config as rc

    notes: dict[str, str] = {}
    groups: dict[str, str] = {}
    section = ""
    for line in inspect.getsource(rc.RiskConfig).splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            section = stripped[2:]
            continue
        m = re.match(r"(\w+):\s*[\w\[\]]+\s*=\s*[^#]+(?:#\s*(.*))?$", stripped)
        if m:
            notes[m.group(1)] = (m.group(2) or "").strip()
            groups[m.group(1)] = section
    return notes, groups


def risk_config_payload() -> dict[str, Any]:
    from deskmind.risk import config as rc

    cfg = rc.RiskConfig()
    notes, groups = risk_field_notes()
    return {
        "config": {f.name: _jsonable(getattr(cfg, f.name)) for f in dataclasses.fields(cfg)},
        "notes": notes,
        "groups": groups,
        "hard_caps": {
            "max_risk_pct": str(rc.HARD_CAP_MAX_RISK_PCT),
            "max_stop_price_pct": str(rc.HARD_CAP_MAX_STOP_PRICE_PCT),
            "max_leverage": rc.HARD_CAP_MAX_LEVERAGE,
            "max_notional_mult": str(rc.HARD_CAP_MAX_NOTIONAL_MULT),
        },
    }


@router.get("/api/dashboard/risk")
async def dashboard_risk() -> dict[str, Any]:
    settings = get_settings()
    status, age = read_trade_status()
    fresh = status is not None and age is not None and age <= TRADE_STATUS_FRESH_S
    mode = settings.trading_mode
    if fresh and status:
        equity = {
            "value": status.get("balance", {}).get("equity"),
            "available": status.get("balance", {}).get("available"),
            "source": (
                "paper executor account (simulated)"
                if status.get("trading_mode") == "paper"
                else "Binance demo/testnet futures balance"
            ),
            "status_age_s": age,
            "reason": None,
        }
    else:
        equity = {
            "value": None,
            "available": None,
            "source": (
                "paper executor account (simulated)"
                if mode == "paper"
                else "Binance demo/testnet futures balance"
            ),
            "status_age_s": age,
            "reason": "trade service not running (no fresh status file)",
        }
    return {
        **risk_config_payload(),
        "trading_mode": mode,
        "equity_by_mode": {
            "paper": {
                "used_when": "TRADING_MODE=paper",
                "source": "paper executor account (simulated)",
                "starting_equity": str(DEFAULT_INITIAL_CASH),
            },
            "testnet": {
                "used_when": "TRADING_MODE=testnet",
                "source": "Binance demo/testnet futures balance (fake money)",
                "starting_equity": None,
            },
            "mainnet": {
                "used_when": "never",
                "source": "real Binance balance is shown read-only on the Account page and is "
                "never used for sizing",
                "starting_equity": None,
            },
        },
        "current_equity": equity,
        "breaker": breaker_payload(status if fresh else None),
    }


def breaker_payload(status: dict[str, Any] | None) -> dict[str, Any]:
    """Breaker state: live from the trade service, else the persisted risk_state.json."""
    from deskmind.api.control import _breaker_file_state

    breaker_file = _breaker_file_state()
    if status is not None:
        return {
            "state": status.get("breaker_state"),
            "source": "trade service status",
            "kill_switch_halted": status.get("kill_switch_halted"),
            "file": breaker_file,
            "reason": None,
        }
    if breaker_file is not None:
        return {
            "state": breaker_file["halt_state"],
            "source": "risk state file (trade service not running)",
            "kill_switch_halted": None,
            "file": breaker_file,
            "reason": None,
        }
    return {
        "state": None,
        "source": None,
        "kill_switch_halted": None,
        "file": None,
        "reason": "trade service not running and no risk_state.json: breaker state unknown",
    }


# ---------------------------------------------------------------- account (read-only mainnet)


def latest_mainnet_snapshot(
    meta_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    files = sorted((meta_dir or META_DIR).glob("mainnet_sync_*.json"))
    if not files:
        return None, None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8")), files[-1].name
    except (OSError, ValueError):
        return None, files[-1].name


@router.get("/api/dashboard/account")
async def dashboard_account() -> dict[str, Any]:
    settings = get_settings()
    snap, name = latest_mainnet_snapshot()
    key_configured = bool(settings.binance_mainnet_readonly_api_key.get_secret_value())
    base = {
        "read_only": True,
        "used_for_sizing": False,
        "key_configured": key_configured,
    }
    if snap is None:
        return {
            **base,
            "snapshot": None,
            "reason": (
                "no read-only sync yet: run `uv run deskmind account sync`"
                if key_configured
                else "no read-only mainnet key configured (BINANCE_MAINNET_READONLY_API_KEY)"
            ),
        }
    perms = snap.get("permissions") or {}
    acct = snap.get("account_info") or None
    balances = []
    if acct:
        for a in acct.get("assets", []):
            if float(a.get("walletBalance", 0) or 0) != 0:
                balances.append(
                    {
                        "asset": a.get("asset"),
                        "wallet_balance": a.get("walletBalance"),
                        "unrealized_pnl": a.get("unrealizedProfit"),
                        "margin_balance": a.get("marginBalance"),
                    }
                )
    return {
        **base,
        "snapshot": name,
        "synced_at": snap.get("synced_at"),
        "permissions": {k: v for k, v in perms.items() if isinstance(v, bool)},
        "futures_read": acct is not None,
        "futures_reason": None
        if acct is not None
        else "Needs futures read access — not enabled (by design)",
        "balances": balances,
        "commission_rates": snap.get("commission_rates") or {},
        "brackets": snap.get("brackets") or {},
        "comparison": snap.get("comparison") or {},
        "reason": None,
    }


# ---------------------------------------------------------------- my Binance history


def _dec_str(v: Any) -> str | None:
    """NUMERIC(38,18) -> plain decimal string without padding zeros or exponent (0E-18 -> 0)."""
    if v is None:
        return None
    out = format(Decimal(v).normalize(), "f")
    return "0" if out in ("-0", "") else out


def _trade_json(t: Any) -> dict[str, Any]:
    return {
        "source": t.source,
        "market": t.market,
        "symbol": t.symbol,
        "side": t.side,
        "price": _dec_str(t.price),
        "qty": _dec_str(t.qty),
        "quote_qty": _dec_str(t.quote_qty),
        "fee": _dec_str(t.fee),
        "fee_asset": t.fee_asset,
        "realized_pnl": _dec_str(t.realized_pnl),
        "time_ms": int(t.time.timestamp() * 1000),
        "order_id": t.order_id,
        "trade_id": t.trade_id,
    }


def _window(start: int | None, end: int | None) -> tuple[Any, Any]:
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(start, UTC) if start is not None else None,
        datetime.fromtimestamp(end, UTC) if end is not None else None,
    )


@router.get("/api/external-trades")
async def external_trades(
    market: str | None = None,
    symbol: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """The user's imported Binance fills (read-only history), newest first."""
    from deskmind.data.external_trades import count_by_market, list_trades
    from deskmind.db.session import session_scope

    s, e = _window(start, end)
    async with session_scope() as session:
        rows = await list_trades(
            session, market=market, symbol=symbol, start=s, end=e, limit=min(limit, 5000)
        )
        totals = await count_by_market(session)
    return {"totals_by_market": totals, "trades": [_trade_json(t) for t in rows]}


@router.get("/api/external-trades/stats")
async def external_trades_stats(
    market: str | None = None,
    symbol: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> dict[str, Any]:
    from deskmind.data.external_trades import list_trades, trade_stats
    from deskmind.db.session import session_scope

    s, e = _window(start, end)
    async with session_scope() as session:
        rows = await list_trades(
            session, market=market, symbol=symbol, start=s, end=e, limit=1_000_000
        )
    return trade_stats(rows)


@router.post("/api/external-trades/upload", dependencies=[Depends(require_csrf)])
async def external_trades_upload(
    request: Request,
    session_data: Annotated[SessionData, Depends(get_current_session)],
    market: str | None = None,
    filename: str = "upload.csv",
) -> dict[str, Any]:
    """Import a Binance trade-history CSV export (raw file body). Login + CSRF required."""
    from deskmind.api.audit import record_audit_action
    from deskmind.api.auth import get_client_ip
    from deskmind.data.external_trades import CsvFormatError, parse_trade_csv, upsert_trades
    from deskmind.db.session import session_scope

    body = await request.body()
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(413, "file larger than 20 MB")
    try:
        detected, rows = parse_trade_csv(body.decode("utf-8-sig"), market)
    except (CsvFormatError, UnicodeDecodeError) as exc:
        raise HTTPException(400, f"could not read {filename}: {exc}") from exc
    async with session_scope() as db:
        inserted, skipped = await upsert_trades(db, rows)
    await record_audit_action(
        action="import_trade_csv",
        user=session_data.username,
        details={"file": filename, "market": detected, "rows": len(rows), "inserted": inserted},
        ip_address=get_client_ip(request),
    )
    return {"market": detected, "rows": len(rows), "inserted": inserted, "duplicates": skipped}


# ---------------------------------------------------------------- backtest lab metadata


@router.get("/api/dashboard/backtest-meta")
async def backtest_meta() -> dict[str, Any]:
    """Split date ranges, runnable strategies and form defaults, straight from the backend."""
    from deskmind.backtest.queue import BacktestSubmitRequest
    from deskmind.backtest.runner import STRATEGY_REGISTRY
    from deskmind.config import BACKTEST_SPLITS

    fields = BacktestSubmitRequest.model_fields
    return {
        "splits": {
            name: {"start": start, "end": end, "ui_allowed": name != "test"}
            for name, (start, end) in BACKTEST_SPLITS.items()
        },
        "strategies": list(STRATEGY_REGISTRY),
        "defaults": {
            "warmup_days": fields["warmup_days"].default,
            "initial_cash": str(fields["initial_cash"].default),
            "symbols": list(get_settings().data_symbols),
        },
    }


# ---------------------------------------------------------------- liquidity universe


@router.get("/api/universe")
async def universe_state(runs: int = 14) -> dict[str, Any]:
    """Symbol lifecycle states and the latest daily summaries (universe_symbols/universe_runs)."""
    from deskmind.universe.ranking import UniverseConfig
    from deskmind.universe.store import PostgresUniverseStore

    settings = get_settings()
    cfg = UniverseConfig.from_settings(settings)
    store = PostgresUniverseStore()
    try:
        recs = await store.load()
        history = await store.runs(min(max(runs, 1), 90))
    except Exception as exc:
        raise HTTPException(503, f"universe tables unavailable: {exc}") from exc
    symbols = sorted(recs.values(), key=lambda r: (r.rank or 10**6, r.symbol))
    return {
        "config": dataclasses.asdict(cfg),
        "trade_symbols": list(settings.trade_symbols),
        "symbols": [r.to_dict() for r in symbols],
        "runs": history,
        "reason": None if recs or history else "the daily universe job has not run yet",
    }
