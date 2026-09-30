"""Deskmind command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import typer

from deskmind.config import Settings, get_settings
from deskmind.health import run_all
from deskmind.logging import configure_logging

app = typer.Typer(help="Deskmind CLI", no_args_is_help=True, add_completion=False)
data_app = typer.Typer(help="Historical market data (Parquet + DuckDB)", no_args_is_help=True)
app.add_typer(data_app, name="data")
live_app = typer.Typer(
    help="Live market data service (WebSocket -> Redis/Postgres)", no_args_is_help=True
)
app.add_typer(live_app, name="live")
backtest_app = typer.Typer(help="Event-driven backtesting engine", no_args_is_help=True)
app.add_typer(backtest_app, name="backtest")
runs_app = typer.Typer(
    help="Backtest run registry and multiple-testing accounting", no_args_is_help=True
)
backtest_app.add_typer(runs_app, name="runs")
risk_app = typer.Typer(
    help="Deterministic risk manager, position sizing, and circuit breakers", no_args_is_help=True
)
app.add_typer(risk_app, name="risk")
trade_app = typer.Typer(
    help="Order execution and live/testnet trade management", no_args_is_help=True
)
app.add_typer(trade_app, name="trade")
account_app = typer.Typer(
    help="Mainnet read-only account and metadata synchronization", no_args_is_help=True
)
app.add_typer(account_app, name="account")
universe_app = typer.Typer(
    help="Liquidity universe: daily recompute, symbol lifecycle, summary", no_args_is_help=True
)
app.add_typer(universe_app, name="universe")

SymbolOpt = typer.Option(None, "--symbol", "-s", help="One symbol from DATA_SYMBOLS")
AllOpt = typer.Option(False, "--all", help="Every symbol in DATA_SYMBOLS")


@app.callback()
def main() -> None:
    """Deskmind: multi-agent advisory system (testnet only)."""
    configure_logging(get_settings().log_level)


@app.command()
def health() -> None:
    """Check config, Postgres and Redis connectivity."""
    results = asyncio.run(run_all())
    for r in results:
        status = "OK  " if r.ok else ("FAIL" if r.required else "WARN")
        typer.echo(f"[{status}] {r.name}: {r.detail}")
    if not all(r.ok for r in results if r.required):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------- data


def resolve_symbols(settings: Settings, symbol: str | None, all_symbols: bool) -> list[str]:
    """Symbols to act on. Only configured symbols are accepted: add new ones to DATA_SYMBOLS."""
    if bool(symbol) == all_symbols:
        raise typer.BadParameter("use exactly one of --symbol X or --all")
    if all_symbols:
        return list(settings.data_symbols)
    sym = (symbol or "").strip().upper()
    if sym not in settings.data_symbols:
        raise typer.BadParameter(
            f"{sym} is not in DATA_SYMBOLS ({', '.join(settings.data_symbols)}); add it there first"
        )
    return [sym]


def _sync(symbols: list[str], start: date | None) -> None:
    from deskmind.data.pipeline import Syncer, make_clients
    from deskmind.data.reconcile import reconcile
    from deskmind.data.storage import Store

    settings = get_settings()

    async def go() -> None:
        fapi, vision = make_clients(settings.binance_market_data_url)
        async with fapi, vision:
            for symbol in symbols:
                store = Store(settings.data_dir, symbol)
                syncer = Syncer(store, fapi, vision)
                results = await syncer.run(start)
                recon = await reconcile(store, fapi)
                filters_path = await syncer.snapshot_filters()
                typer.echo(f"\n== {symbol}")
                typer.echo(
                    f"{'dataset':<16}{'checked':>8}{'skipped':>8}{'monthly':>8}{'daily':>7}"
                    f"{'rest':>6}{'rows+':>10}{'missing':>9}"
                )
                for r in results:
                    typer.echo(
                        f"{r.key:<16}{r.months_checked:>8}{r.months_skipped:>8}"
                        f"{r.monthly_zips:>8}{r.daily_zips:>7}{r.rest_ranges:>6}"
                        f"{r.rows_added:>10,}{r.missing_candles:>9,}"
                    )
                typer.echo(
                    f"reconcile: zero-trade re-checked {sum(recon.zero_trade_checked.values())}, "
                    f"rows replaced by REST {sum(recon.replaced.values())}, "
                    f"cross-tf re-checked {sum(recon.cross_tf_rechecked.values())}, "
                    f"confirmed exchange-side {sum(recon.cross_tf_confirmed.values())}"
                )
                typer.echo(f"filters snapshot: {filters_path}")
            typer.echo(
                f"\nrequests: data.binance.vision={vision.request_count}, fapi={fapi.request_count}"
            )

    asyncio.run(go())


@data_app.command()
def download(
    symbol: str | None = SymbolOpt,
    all_symbols: bool = AllOpt,
    start: str = typer.Option("2023-01-01", help="First UTC date to download (YYYY-MM-DD)"),
) -> None:
    """Download history from START to the last closed candle (skips what is already stored)."""
    _sync(resolve_symbols(get_settings(), symbol, all_symbols), date.fromisoformat(start))


@data_app.command()
def update(symbol: str | None = SymbolOpt, all_symbols: bool = AllOpt) -> None:
    """Fetch only what is missing, up to the last closed candle."""
    _sync(resolve_symbols(get_settings(), symbol, all_symbols), None)


@data_app.command()
def verify(symbol: str | None = SymbolOpt, all_symbols: bool = AllOpt) -> None:
    """Run data-quality checks; writes data/reports/verify_{date}.md."""
    from deskmind.data.storage import Store
    from deskmind.data.verify import annotate_cross_symbol, verify_store, write_run_report

    settings = get_settings()
    symbols = resolve_symbols(settings, symbol, all_symbols)
    reports = [verify_store(Store(settings.data_dir, s)) for s in symbols]
    annotate_cross_symbol(reports)
    path = write_run_report(settings.data_dir / "reports", reports)
    for rep in reports:
        typer.echo(f"\n== {rep.symbol}: {rep.status}")
        for d in rep.datasets:
            typer.echo(
                f"[{d.status}] {d.key:<16} rows={d.rows:>9,}  gaps={len(d.gaps):>3}  "
                f"missing={d.missing_total:>5}"
            )
            for c in d.checks:
                if c.status in ("WARN", "FAIL"):
                    typer.echo(f"         {c.status} {c.name}: {c.detail}")
            for g in d.gaps if d.key != "funding" else []:
                where = ", ".join(g.also_in) if g.also_in else "this symbol only"
                typer.echo(
                    f"         gap {g.first_missing:%Y-%m-%d %H:%M} -> "
                    f"{g.last_missing:%Y-%m-%d %H:%M} ({g.missing}) also in: {where}"
                )
        for c in rep.cross_tf:
            typer.echo(
                f"[{c.status}] {c.source} -> {c.target}: compared={c.compared:,} "
                f"mismatch={c.mismatches} confirmed_exchange_side={c.confirmed_source} "
                f"vol_mismatch={c.volume_mismatches} missing_in_target={c.missing_in_target}"
            )
        for m in rep.mark_last:
            typer.echo(
                f"[INFO] mark vs last {m.timeframe}: flagged={m.flagged:,} "
                f"(>{m.threshold:.0%}) in {len(m.episodes)} episodes, "
                f"median={m.median_dev:.4%} max={m.max_dev:.2%}"
            )
    overall = (
        "FAIL"
        if any(r.status == "FAIL" for r in reports)
        else ("WARN" if any(r.status == "WARN" for r in reports) else "PASS")
    )
    typer.echo(f"\noverall: {overall}   report: {path}")
    if overall == "FAIL":
        raise typer.Exit(code=1)


@data_app.command()
def info(symbol: str | None = SymbolOpt, all_symbols: bool = AllOpt) -> None:
    """Row counts, date range and size on disk per dataset."""
    from deskmind.data.datasets import DEFAULT_DATASETS
    from deskmind.data.storage import Store, describe

    settings = get_settings()
    grand = 0
    for sym in resolve_symbols(settings, symbol, all_symbols):
        store = Store(settings.data_dir, sym)
        typer.echo(
            f"\n== {sym}\n{'dataset':<16}{'files':>6}{'rows':>11}  "
            f"{'first (UTC)':<17}  {'last (UTC)':<17}{'size':>10}"
        )
        total = 0
        for r in describe(store, DEFAULT_DATASETS):
            total += r["bytes"]
            first = r["first"].strftime("%Y-%m-%d %H:%M") if r["first"] else "-"
            last = r["last"].strftime("%Y-%m-%d %H:%M") if r["last"] else "-"
            typer.echo(
                f"{r['dataset']:<16}{r['files']:>6}{r['rows']:>11,}  {first:<17}  {last:<17}"
                f"{r['bytes'] / 1e6:>8.2f}MB"
            )
        meta = sorted(store.meta_dir.glob(f"{store.symbol}_filters_*.json"))
        typer.echo(f"{'total':<67}{total / 1e6:>8.2f}MB")
        typer.echo(f"filters snapshots: {len(meta)} (latest: {meta[-1].name if meta else '-'})")
        grand += total
    typer.echo(f"\nall symbols: {grand / 1e6:.2f}MB")


# ---------------------------------------------------------------- agents

agents_app = typer.Typer(help="Analyst agents (deterministic)", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
AGENTS = ("trend", "ict_smc")


def _check_agent(name: str) -> None:
    if name not in AGENTS:
        raise typer.BadParameter(f"unknown agent {name!r}; available: {', '.join(AGENTS)}")


@agents_app.command("run")
def agents_run(
    agent: str = typer.Argument(..., help="Agent name (trend, ict_smc)"),
    duration: float = typer.Option(0, help="Stop after N seconds (0 = until Ctrl+C)"),
) -> None:
    """Run an agent live: warm up, then process every deskmind:bar_close event."""
    _check_agent(agent)
    if agent == "trend":
        from deskmind.agents.trend.runner import run_live

        asyncio.run(run_live(get_settings(), duration or None))
    else:
        from deskmind.agents.ict_smc.runner import run_live

        asyncio.run(run_live(get_settings(), duration or None))


@agents_app.command("backfill")
def agents_backfill(
    agent: str = typer.Argument(..., help="Agent name (trend, ict_smc)"),
    symbol: str | None = SymbolOpt,
    all_symbols: bool = AllOpt,
    start: str = typer.Option(..., help="UTC start, e.g. 2026-06-28"),
    end: str = typer.Option("", help="UTC end (default: now)"),
    debug_report_days: float = typer.Option(
        0.0,
        help="Also store full reports for the last N days (debug). Default: "
        "items + transitions only; state as of any T is rebuilt from them.",
    ),
) -> None:
    """Replay history through the agent and store its point-in-time record for [start, end]."""
    _check_agent(agent)
    import time
    from datetime import UTC, datetime

    from deskmind.agents.trend.runner import parse_day
    from deskmind.api.candles import PostgresRecentCandles
    from deskmind.db.session import create_engine

    settings = get_settings()
    symbols = resolve_symbols(settings, symbol, all_symbols)
    start_ms = parse_day(start)
    end_ms = parse_day(end) if end else int(datetime.now(UTC).timestamp() * 1000)

    async def go() -> None:
        engine = create_engine(settings)
        try:
            for sym in symbols:
                t0 = time.time()
                if agent == "trend":
                    from deskmind.agents.trend.runner import backfill as trend_backfill
                    from deskmind.agents.trend.store import PostgresTrendStore

                    r = await trend_backfill(
                        sym,
                        list(settings.live_timeframes),
                        start_ms,
                        end_ms,
                        data_dir=settings.data_dir,
                        recent=PostgresRecentCandles(engine),
                        store=PostgresTrendStore(engine),
                        debug_report_days=debug_report_days,
                    )
                    kinds = [
                        "swing_high",
                        "swing_low",
                        "bos",
                        "choch",
                        "sweep",
                        "trendline_support",
                        "trendline_resistance",
                    ]
                else:
                    from deskmind.agents.ict_smc.runner import backfill as ict_backfill
                    from deskmind.agents.ict_smc.store import PostgresIctSmcStore

                    r = await ict_backfill(
                        sym,
                        list(settings.live_timeframes),
                        start_ms,
                        end_ms,
                        data_dir=settings.data_dir,
                        recent=PostgresRecentCandles(engine),
                        store=PostgresIctSmcStore(engine),
                        debug_report_days=debug_report_days,
                    )
                    kinds = [
                        "bsl",
                        "ssl",
                        "fvg_bullish",
                        "fvg_bearish",
                        "ob_bullish",
                        "ob_bearish",
                        "breaker_bullish",
                        "breaker_bearish",
                        "liquidity_void",
                    ]

                typer.echo(
                    f"\n== {sym}: {r['reports']} reports, {r['items']} items"
                    + (f", {r['transitions']} transitions" if "transitions" in r else "")
                    + f" ({time.time() - t0:.1f}s)"
                )
                typer.echo(f"{'tf':<5}" + "".join(f"{k:>20}" for k in kinds))
                for tf in settings.live_timeframes:
                    c = r["counts"].get(tf, {})
                    typer.echo(f"{tf:<5}" + "".join(f"{c.get(k, 0):>20}" for k in kinds))
                if r.get("timings"):
                    typer.echo(
                        "timings: " + ", ".join(f"{k} {v}s" for k, v in r["timings"].items())
                    )
        finally:
            await engine.dispose()

    asyncio.run(go())


@agents_app.command("check")
def agents_check(
    agent: str = typer.Argument(..., help="Agent name (trend, ict_smc)"),
    symbol: str | None = SymbolOpt,
    all_symbols: bool = AllOpt,
    bars: int = typer.Option(4000, help="Most recent candles per symbol/timeframe"),
    cuts: int = typer.Option(20, help="Random truncation points per symbol/timeframe"),
    offset: int = typer.Option(300, help="Start offset for the convergence check"),
) -> None:
    """No-look-ahead truncation test + start-point convergence on real stored candles."""
    _check_agent(agent)
    from datetime import UTC, datetime

    from deskmind.agents.candles import load_bars

    if agent == "trend":
        from deskmind.agents.trend.checks import convergence_check, truncation_check
    else:
        from deskmind.agents.ict_smc.checks import convergence_check, truncation_check

    settings = get_settings()
    symbols = resolve_symbols(settings, symbol, all_symbols)
    now = int(datetime.now(UTC).timestamp() * 1000)
    ok = True
    typer.echo(
        f"{'symbol':<9}{'tf':<5}{'candles':>8}{'cuts':>6}{'passed':>8}{'items compared':>16}"
        f"{'converged after':>17}"
    )
    for sym in symbols:
        for tf in settings.live_timeframes:
            data = asyncio.run(load_bars(settings.data_dir, None, sym, tf, end_ms=now, last_n=bars))
            r = truncation_check(data, n_cuts=cuts)
            conv = convergence_check(data, offset)
            ok = ok and r.passed == r.cuts
            c = conv["converged_after_bars"]
            typer.echo(
                f"{sym:<9}{tf:<5}{len(data):>8}{r.cuts:>6}{r.passed:>8}{r.items_compared:>16}"
                f"{(str(c) + ' bars') if c is not None else 'never':>17}"
            )
            for f in r.failures[:3]:
                typer.echo(f"    FAIL {f}")
    typer.echo(f"no-look-ahead: {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------- api


@app.command()
def api(
    port: int = typer.Option(8010, help="Port (the API always binds to 127.0.0.1 only)"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)"),
) -> None:
    """Run the read-only API for the web app on http://127.0.0.1:PORT (docs at /docs)."""
    import uvicorn

    uvicorn.run(
        "deskmind.api.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        reload=reload,
        log_level=get_settings().log_level.lower(),
    )


# ---------------------------------------------------------------- live


@live_app.command("run")
def live_run(
    duration: float = typer.Option(
        0, help="Stop after this many seconds (0 = run until Ctrl+C / SIGTERM)"
    ),
) -> None:
    """Start the live service in the foreground."""
    from deskmind.data.live.commands import run_service

    asyncio.run(run_service(get_settings(), duration or None))


@live_app.command("status")
def live_status() -> None:
    """Per symbol/tf: last closed candle, buffer size, staleness; mark/OI age; connection."""
    from deskmind.data.live.commands import status

    asyncio.run(status(get_settings(), typer.echo))


@live_app.command("tail")
def live_tail(
    history: int = typer.Option(0, help="First print this many past events from the log"),
) -> None:
    """Print bar_close events as they arrive (Ctrl+C to stop)."""
    from deskmind.data.live.commands import tail

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(tail(get_settings(), typer.echo, history))


@live_app.command("verify")
def live_verify(
    hours: float = typer.Option(1.0, help="Check the last N hours"),
    since: str = typer.Option("", help="Or an explicit UTC start, e.g. 2026-09-26T08:00"),
) -> None:
    """Compare live-stored candles (Redis + Postgres) with fresh REST; check event sequence."""
    from datetime import UTC, datetime

    from deskmind.data.live.commands import parse_since, verify

    ok = asyncio.run(
        verify(get_settings(), typer.echo, parse_since(hours, since or None), datetime.now(UTC))
    )
    typer.echo(f"result: {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise typer.Exit(code=1)


@live_app.command("disconnect")
def live_disconnect(
    seconds: int = typer.Option(120, help="How long the running service's WebSocket stays down"),
) -> None:
    """Test hook: make the running service drop its WebSocket and stay offline for SECONDS."""
    from deskmind.data.live.commands import block_ws

    until = asyncio.run(block_ws(get_settings(), seconds))
    typer.echo(f"WebSocket blocked for {seconds}s (until epoch ms {until})")


# ---------------------------------------------------------------- backtest


@backtest_app.command("run")
def backtest_run_cmd(
    strategy: str = typer.Option("trend_follow_v1", "--strategy", help="Strategy name"),
    split: str = typer.Option("train", "--split", help="Data split: train, validation, test"),
    symbol: str | None = SymbolOpt,
    all_symbols: bool = AllOpt,
    start: str | None = typer.Option(None, "--start", help="Start date (YYYY-MM-DD)"),
    end: str | None = typer.Option(None, "--end", help="End date (YYYY-MM-DD)"),
    unlock_test: bool = typer.Option(False, "--unlock-test", help="Unlock the locked test split"),
    confirm_test_unlock: str | None = typer.Option(
        None, "--confirm-test-unlock", help="Type CONFIRM to confirm test unlock"
    ),
    random_seeds: int = typer.Option(
        1000, "--random-seeds", help="Number of random-entry baseline seeds"
    ),
    warmup_days: int = typer.Option(
        30, "--warmup-days", help="Warm-up days for indicator/trend agent initialization"
    ),
    disable_circuit_breakers: bool = typer.Option(
        False,
        "--disable-circuit-breakers",
        help="Disable circuit breakers for diagnostic comparison",
    ),
    notes: str | None = typer.Option(None, "--notes", help="Audit notes for this run"),
    universe: str = typer.Option(
        "auto",
        "--universe",
        help="Point-in-time universe filter: auto (use if history covers the period), "
        "require (refuse without it), off",
    ),
) -> None:
    """Run an event-driven backtest with realistic execution and incremental agents."""
    from deskmind.backtest.runner import run_backtest

    settings = get_settings()
    if symbol is None and not all_symbols:
        symbols = list(settings.data_symbols)
    else:
        symbols = resolve_symbols(settings, symbol, all_symbols)

    typer.echo(f"Starting backtest: strategy={strategy}, split={split}, symbols={symbols}")
    if disable_circuit_breakers:
        typer.secho(
            "DIAGNOSTIC RUN — SHADOW MODE: Circuit breakers disabled (observational only).",
            fg=typer.colors.YELLOW,
            bold=True,
        )
    try:
        res = asyncio.run(
            run_backtest(
                strategy_name=strategy,
                split=split,
                symbols=symbols,
                start_date=start,
                end_date=end,
                unlock_test=unlock_test,
                confirm_test_unlock=confirm_test_unlock,
                random_seeds=random_seeds,
                warmup_days=warmup_days,
                disable_circuit_breakers=disable_circuit_breakers,
                notes=notes,
                universe_mode=universe,  # type: ignore[arg-type]
            )
        )
    except PermissionError as e:
        typer.secho(f"REFUSAL: {e}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1) from None
    except Exception as e:
        typer.secho(f"ERROR: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    m = res["metrics"]
    rb = res["random_baseline"]
    typer.echo(f"\nBacktest completed: run_id={res['run_id']}")
    typer.echo(f"Reports saved to: {res['reports_dir']}")
    typer.echo(f"Trades: {res['trades_count']}")
    typer.echo(f"Net Return: ${m.net_return_usd:+,.2f} ({m.net_return_pct:+.2f}%)")
    typer.echo(f"Max Drawdown: -{m.max_drawdown_pct:.2f}% (${m.max_drawdown_usd:,.2f})")
    typer.echo(f"Sharpe Ratio: {m.sharpe_ratio:.2f}")
    typer.echo(f"Profit Factor: {m.profit_factor:.2f}")
    typer.echo(f"Win Rate: {m.win_rate_pct:.1f}%")
    typer.echo(f"Average R: {m.avg_r:+.2f}R")
    typer.echo(f"Random-Entry Baseline: {rb.percentile:.1f}th percentile ({rb.verdict.upper()})")


@runs_app.command("list")
def runs_list_cmd() -> None:
    """List all recorded backtest runs and multiple-testing accounting counts."""
    from deskmind.backtest.registry import list_runs_local

    runs = list_runs_local()
    if not runs:
        typer.echo("No backtest runs recorded yet.")
        return

    # Count variants per strategy
    strat_counts: dict[str, int] = {}
    for r in runs:
        s_name = r.get("strategy_name", "unknown")
        strat_counts[s_name] = strat_counts.get(s_name, 0) + 1

    typer.echo("=== Strategy Variants Tried (Multiple-Testing Accounting) ===")
    for s_name, count in sorted(strat_counts.items()):
        typer.echo(f"  {s_name}: {count} run(s) recorded")
    typer.echo("")

    typer.echo(
        f"{'run_id':<38} {'strategy':<18} {'split':<10} {'trades':>7} "
        f"{'win rate':>9} {'net ret':>10} {'sharpe':>7} {'max DD':>8}"
    )
    typer.echo("-" * 114)
    for r in runs:
        m = r.get("metrics", {})
        trades = m.get("total_trades", 0)
        win_rate = f"{m.get('win_rate_pct', 0.0):.1f}%"
        ret = f"{m.get('net_return_pct', 0.0):+.1f}%"
        sharpe = f"{m.get('sharpe_ratio', 0.0):.2f}"
        max_dd = f"{m.get('max_drawdown_pct', 0.0):.1f}%"
        typer.echo(
            f"{r['run_id']:<38} {r.get('strategy_name', ''):<18} {r.get('split', ''):<10} "
            f"{trades:>7} {win_rate:>9} {ret:>10} {sharpe:>7} {max_dd:>8}"
        )


@runs_app.command("show")
def runs_show_cmd(run_id: str = typer.Argument(..., help="Run ID to show")) -> None:
    """Show details and full metrics for a recorded backtest run."""
    from deskmind.backtest.registry import list_runs_local

    runs = list_runs_local()
    match = next((r for r in runs if r.get("run_id") == run_id), None)
    if not match:
        typer.secho(f"Run {run_id!r} not found in registry.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.echo(f"=== Backtest Run: {run_id} ===")
    typer.echo(f"Strategy: {match.get('strategy_name')} (v{match.get('strategy_version')})")
    typer.echo(
        f"Split: {match.get('split')} ({match.get('start_date')} to {match.get('end_date')})"
    )
    typer.echo(f"Symbols: {', '.join(match.get('symbols', []))}")
    typer.echo(f"Created At: {match.get('created_at')}")
    typer.echo(f"Git Commit: {match.get('git_commit')}")

    m = match.get("metrics", {})
    typer.echo("\n--- Key Metrics ---")
    typer.echo(
        f"Net Return: ${m.get('net_return_usd', 0):+,.2f} ({m.get('net_return_pct', 0):+.2f}%)"
    )
    typer.echo(f"Sharpe Ratio: {m.get('sharpe_ratio', 0):.2f}")
    typer.echo(f"Profit Factor: {m.get('profit_factor', 0):.2f}")
    typer.echo(
        f"Max Drawdown: -{m.get('max_drawdown_pct', 0):.2f}% (${m.get('max_drawdown_usd', 0):,.2f})"
    )
    typer.echo(f"Win Rate: {m.get('win_rate_pct', 0):.1f}% ({m.get('total_trades', 0)} trades)")
    typer.echo(f"Average R: {m.get('avg_r', 0):+.2f}R")

    rb = match.get("random_baseline", {})
    if rb:
        pct = rb.get("percentile", 0)
        verd = rb.get("verdict", "").upper()
        typer.echo(f"Random-Entry Baseline: {pct:.1f}th percentile ({verd})")


# ---------------------------------------------------------------- risk


@risk_app.command("check")
def risk_check_cmd(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Symbol (e.g. BTCUSDT)"),
    side: str = typer.Option(..., "--side", help="Order side: long or short"),
    entry: float = typer.Option(..., "--entry", "-e", help="Expected entry price"),
    stop: float = typer.Option(..., "--stop", help="Stop loss price"),
    target: float | None = typer.Option(None, "--target", "-t", help="Take profit target price"),
    equity: float = typer.Option(10000.0, "--equity", help="Account equity in USD"),
    available_balance: float | None = typer.Option(
        None, "--available-balance", help="Available balance in USD (defaults to equity)"
    ),
    atr: float | None = typer.Option(
        None, "--atr", help="ATR for entry timeframe (defaults to 1.5% of entry)"
    ),
    funding_rate: float = typer.Option(
        0.0001, "--funding-rate", help="Current 8h funding rate (e.g. 0.0001 = 0.01%)"
    ),
    next_funding_minutes: int = typer.Option(
        120, "--next-funding-minutes", help="Minutes until next funding event"
    ),
    halt_state: str = typer.Option("normal", "--halt-state", help="Account breaker state"),
    peak_equity: float | None = typer.Option(None, "--peak-equity", help="Peak historical equity"),
    open_positions: list[str] = typer.Option(
        None, "--open-position", help="Symbols of currently open positions"
    ),
) -> None:
    """Evaluate a trade intent through the deterministic Risk Manager."""
    from deskmind.backtest.filters import load_symbol_filters
    from deskmind.risk import (
        AccountState,
        IntentPayload,
        MarketState,
        RiskConfig,
        evaluate,
    )

    settings = get_settings()
    sym = symbol.strip().upper()
    s_side = side.strip().lower()

    if s_side not in ("long", "short"):
        typer.secho("Error: --side must be 'long' or 'short'", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    entry_d = Decimal(str(entry))
    stop_d = Decimal(str(stop))
    target_d = Decimal(str(target)) if target is not None else None
    equity_d = Decimal(str(equity))
    avail_d = Decimal(str(available_balance)) if available_balance is not None else equity_d
    peak_d = Decimal(str(peak_equity)) if peak_equity is not None else equity_d
    atr_val = Decimal(str(atr)) if atr is not None else (entry_d * Decimal("0.015"))
    # Load exchange filters
    sym_filters = load_symbol_filters(settings.data_dir, sym)

    intent = IntentPayload(
        symbol=sym,
        side=s_side,  # type: ignore[arg-type]
        expected_entry=entry_d,
        stop_loss=stop_d,
        take_profit=target_d or Decimal("0"),
        strategy_name="cli_manual",
        strategy_version="1.0",
    )
    account = AccountState(
        equity=equity_d,
        available_balance=avail_d,
        open_positions=open_positions or [],
        today_realized_pnl=Decimal("0"),
        peak_equity=peak_d,
        consecutive_losses=0,
        halt_state=halt_state,
        start_of_day_equity=equity_d,
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    market = MarketState(
        mark_price=entry_d,
        atr_by_tf={"15m": atr_val, "5m": atr_val, "1h": atr_val},
        next_funding_time_ms=now_ms + (next_funding_minutes * 60 * 1000),
        current_funding_rate=Decimal(str(funding_rate)),
        filters=sym_filters,
        is_live=False,
    )
    config = RiskConfig()

    decision = evaluate(intent, account, market, config)

    # Display results
    is_approved = decision.status == "approved"
    header_color = typer.colors.GREEN if is_approved else typer.colors.RED

    typer.echo("=" * 80)
    typer.secho(
        f"RISK DECISION: {decision.status.upper()}",
        fg=header_color,
        bold=True,
    )
    typer.echo("=" * 80)
    typer.echo(
        f"Intent:       {sym} {s_side.upper()} @ {entry_d:,.4f} | Stop: {stop_d:,.4f} | "
        f"Target: {f'{target_d:,.4f}' if target_d else 'None'}"
    )
    typer.echo(
        f"Account:      Equity: ${equity_d:,.2f} | Avail: ${avail_d:,.2f} | Halt State: {halt_state}"
    )
    typer.echo(
        f"Market:       Mark: {entry_d:,.4f} | ATR: {atr_val:,.4f} | "
        f"Funding: {funding_rate * 100:+.4f}% in {next_funding_minutes}m"
    )
    typer.echo("-" * 80)
    typer.echo(f"Position Qty:       {decision.qty} {sym}")
    typer.echo(f"Leverage:           {decision.leverage}x (Isolated)")
    typer.echo(
        f"Notional:           ${decision.notional:,.2f} "
        f"({(decision.notional / equity_d * 100):.1f}% of equity)"
        if equity_d > 0
        else f"Notional:           ${decision.notional:,.2f}"
    )
    typer.echo(f"Isolated Margin:    ${decision.isolated_margin:,.2f}")
    typer.echo(f"Liquidation Price:  ${decision.liquidation_price:,.4f}")
    typer.echo(
        f"Stop Distance:      ${decision.stop_distance_usd:,.4f} "
        f"({decision.stop_distance_pct * 100:.2f}% | {decision.stop_distance_atr:.2f}x ATR)"
    )
    typer.echo(f"Estimated Costs:    {decision.expected_costs_r:.4f} R")
    typer.echo(f"Net Reward:Risk:    {decision.net_reward_risk:.2f} R (>= 1.50 required)")
    typer.echo(
        f"Allowed Risk:       ${decision.allowed_risk_usd:,.2f} | Actual Risk: ${decision.actual_risk_usd:,.2f}"
    )
    typer.echo("-" * 80)
    typer.echo("Evaluated Checks:")
    for r in decision.reasons:
        tag = "[PASS]" if r.passed else "[FAIL]"
        color = typer.colors.GREEN if r.passed else typer.colors.RED
        typer.secho(f"  {tag} {r.code:<26}: {r.message}", fg=color)
    typer.echo("=" * 80)


@risk_app.command("status")
def risk_status_cmd(
    state_file: Path | None = typer.Option(
        None, "--state-file", help="Path to circuit breaker state file"
    ),
) -> None:
    """Show current risk manager & circuit breaker status."""
    from deskmind.risk.breakers import CircuitBreakerTracker

    settings = get_settings()
    path = state_file or (settings.data_dir / "risk_state.json")
    tracker = CircuitBreakerTracker.load_state(path)

    dd_pct = Decimal("0")
    if tracker.peak_equity > Decimal("0"):
        dd_pct = (
            (tracker.peak_equity - tracker.start_of_day_equity) / tracker.peak_equity
        ) * Decimal("100")

    typer.echo("=" * 70)
    typer.secho("RISK MANAGER & CIRCUIT BREAKER STATUS", bold=True)
    typer.echo("=" * 70)
    typer.echo(f"Current Halt State:      {tracker.halt_state.upper()}")
    typer.echo(f"Peak Equity:             ${tracker.peak_equity:,.2f}")
    typer.echo(f"Start-of-Day Equity:     ${tracker.start_of_day_equity:,.2f}")
    typer.echo(f"Today's Realized PnL:    ${tracker.today_realized_pnl:+,.2f}")
    typer.echo(f"Current Drawdown:        {dd_pct:.2f}% from peak")
    typer.echo(f"Consecutive Losses:      {tracker.consecutive_losses} (cooldown at 4)")
    typer.echo(f"Cooldown Active:         {'YES' if tracker.cooldown_until_ms > 0 else 'NO'}")
    typer.echo(f"Logged Transitions:      {len(tracker.transitions)}")
    typer.echo(f"State File:              {path}")
    typer.echo("=" * 70)


@risk_app.command("reset")
def risk_reset_cmd(
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Typed confirmation flag required to reset 20% drawdown full stop",
    ),
    state_file: Path | None = typer.Option(
        None, "--state-file", help="Path to circuit breaker state file"
    ),
) -> None:
    """Manually reset the 20% drawdown full stop breaker (requires --confirm)."""
    from deskmind.risk.breakers import CircuitBreakerTracker

    if not confirm:
        typer.secho(
            "ERROR: Typed confirmation flag --confirm is required to reset the 20% full stop breaker.",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)

    settings = get_settings()
    path = state_file or (settings.data_dir / "risk_state.json")
    tracker = CircuitBreakerTracker.load_state(path)

    if tracker.halt_state != "full_stop":
        typer.echo(
            f"Breaker state is '{tracker.halt_state}'. Reset only applies when state is 'full_stop'."
        )
        return

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    tracker.reset_full_stop(current_equity=tracker.start_of_day_equity, timestamp_ms=now_ms)
    tracker.save_state(path)
    typer.secho(
        "SUCCESS: 20% drawdown full stop reset. Operational state restored to NORMAL.",
        fg=typer.colors.GREEN,
        bold=True,
    )


# ---------------------------------------------------------------- trade


@trade_app.command("run")
def trade_run_cmd(
    strategy: str = typer.Option(
        "trend_follow_v1", "--strategy", help="Strategy: trend_follow_v1 or plumbing_stress_v1"
    ),
    mode: str | None = typer.Option(None, "--mode", "-m", help="Trading mode: paper or testnet"),
    approval: str | None = typer.Option(None, "--approval", help="Approval mode: auto or manual"),
    duration: float | None = typer.Option(None, "--duration", "-d", help="Run duration in seconds"),
) -> None:
    """Run trade execution service (consumes deskmind:agent_report)."""
    import asyncio

    from deskmind.execution.service import ExecutionService

    settings = get_settings()
    if mode:
        settings.trading_mode = mode.lower()
    if approval:
        settings.approval_mode = approval.lower()

    service = ExecutionService(settings=settings, strategy_name=strategy)

    async def _runner() -> None:
        if duration:
            asyncio.get_running_loop().call_later(duration, service.stop)
        await service.start()

    typer.echo(
        f"Starting execution service [{service.trading_mode} | {service.approval_mode}] with strategy '{strategy}'..."
    )
    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        service.stop()
        typer.echo("Execution service stopped by user.")


@trade_app.command("status")
def trade_status_cmd() -> None:
    """Show live status: positions, orders, circuit breakers, 24h metrics, and stream health."""
    import json

    from deskmind.execution.service import STATUS_FILE

    if not STATUS_FILE.exists():
        typer.secho(
            "No active execution status found. Service might not be running.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Run: uv run deskmind trade run")
        return

    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        typer.secho(f"Error reading status file: {e}", fg=typer.colors.RED)
        return

    typer.echo("=" * 70)
    typer.secho(" DESKMIND TRADE STATUS ", fg=typer.colors.CYAN, bold=True)
    typer.echo("=" * 70)

    # General info
    status_str = "RUNNING" if data.get("is_running") else "STOPPED"
    status_color = typer.colors.GREEN if data.get("is_running") else typer.colors.YELLOW
    typer.echo("Service State:           ", nl=False)
    typer.secho(status_str, fg=status_color, bold=True)
    typer.echo(f"Trading Mode:            {data.get('trading_mode', 'unknown').upper()}")
    typer.echo(f"Approval Mode:           {data.get('approval_mode', 'unknown').upper()}")
    typer.echo(f"Active Strategy:         {data.get('strategy', 'unknown')}")
    typer.echo(f"Stream Health:           {data.get('stream_health', 'unknown')}")

    # Breakers & Kill switch
    breaker = data.get("breaker_state", "normal")
    breaker_color = typer.colors.GREEN if breaker == "normal" else typer.colors.RED
    typer.echo("Circuit Breaker:         ", nl=False)
    typer.secho(breaker.upper(), fg=breaker_color, bold=True)

    if data.get("kill_switch_halted"):
        typer.secho(
            "KILL SWITCH:             HALTED (All trading disabled)", fg=typer.colors.RED, bold=True
        )

    # Balances
    bal = data.get("balance", {})
    typer.echo("-" * 70)
    typer.echo(
        f"Equity: ${bal.get('equity', '0')} | Available: ${bal.get('available', '0')} | Unrealized PnL: ${bal.get('unrealized_pnl', '0')}"
    )

    # Positions
    positions = data.get("positions", [])
    typer.echo("-" * 70)
    typer.secho(f"OPEN POSITIONS ({len(positions)}):", bold=True)
    if not positions:
        typer.echo("  No active positions.")
    else:
        for p in positions:
            stop_str = f"SL: {p.get('stop_loss')}" if p.get("stop_loss") else "NO STOP (EMERGENCY!)"
            tp_str = f"TP: {p.get('take_profit')}" if p.get("take_profit") else "No TP"
            stop_color = typer.colors.GREEN if p.get("stop_loss") else typer.colors.RED
            typer.echo(
                f"  • {p['symbol']} {p['side'].upper()} | Qty: {p['qty']} | Entry: {p['entry_price']} | Mark: {p['mark_price']} | PnL: ${p['unrealized_pnl']} | ",
                nl=False,
            )
            typer.secho(f"{stop_str} | {tp_str}", fg=stop_color)

    # Orders
    orders = data.get("open_orders", [])
    typer.echo("-" * 70)
    typer.secho(f"OPEN ORDERS ({len(orders)}):", bold=True)
    if not orders:
        typer.echo("  No open orders.")
    else:
        for o in orders:
            typer.echo(
                f"  • {o['symbol']} {o['side'].upper()} {o['order_type']} | Qty: {o['qty']} | Price: {o.get('price')} | Stop: {o.get('stop_price')} | ID: {o['order_id']}"
            )

    # 24h Stats
    stats = data.get("stats_24h", {})
    typer.echo("-" * 70)
    typer.secho("24H PERFORMANCE & ACTIVITY:", bold=True)
    typer.echo(f"  Intents Evaluated:     {stats.get('intents_count', 0)}")
    typer.echo(f"  Approvals Granted:     {stats.get('approvals_count', 0)}")
    typer.echo(
        f"  Orders Sent/Filled:    {stats.get('orders_sent', 0)} / {stats.get('orders_filled', 0)}"
    )
    typer.echo(f"  Orders Rejected:       {stats.get('orders_rejected', 0)}")
    typer.echo(f"  Reconciliation Fixes:  {stats.get('reconciliation_fixes', 0)}")
    if stats.get("time_since_last_order_s") is not None:
        typer.echo(f"  Time Since Last Order: {stats['time_since_last_order_s']}s")
    if stats.get("time_since_last_report_s") is not None:
        typer.echo(f"  Time Since Last Event: {stats['time_since_last_report_s']}s")

    # Warnings
    warnings = data.get("warnings", [])
    if warnings:
        typer.echo("-" * 70)
        for w in warnings:
            typer.secho(f"  {w}", fg=typer.colors.RED, bold=True)

    typer.echo("=" * 70)


@trade_app.command("kill")
def trade_kill_cmd(
    confirm: bool = typer.Option(
        False, "--confirm", help="Confirmation flag required to execute kill switch"
    ),
    reason: str = typer.Option(
        "Manual kill command from CLI", "--reason", "-r", help="Reason for kill switch"
    ),
) -> None:
    """Emergency kill switch: cancel all orders, market close all positions, and halt trading."""
    import asyncio

    from deskmind.execution.binance_client import BinanceFuturesClient
    from deskmind.execution.binance_testnet import BinanceTestnetExecutor
    from deskmind.execution.kill_switch import KillSwitch
    from deskmind.execution.paper import PaperExecutor
    from deskmind.risk.breakers import CircuitBreakers

    if not confirm:
        typer.secho(
            "ERROR: Typed confirmation flag --confirm is required to activate the kill switch.",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)

    settings = get_settings()
    mode = settings.trading_mode.lower()

    if mode == "paper":
        executor = PaperExecutor()
    else:
        client = BinanceFuturesClient(
            api_key=settings.binance_api_key.get_secret_value() if settings.binance_api_key else "",
            secret_key=settings.binance_secret_key.get_secret_value()
            if settings.binance_secret_key
            else "",
            base_url=settings.binance_futures_base_url,
        )
        executor = BinanceTestnetExecutor(client=client, settings=settings)

    breakers = CircuitBreakers()
    kill_switch = KillSwitch(executor=executor, tracker=breakers.tracker)

    typer.secho("ACTIVATING KILL SWITCH...", fg=typer.colors.RED, bold=True)
    res = asyncio.run(kill_switch.trigger_halt(reason=reason))
    asyncio.run(executor.close_connections())

    cancelled = len(res.get("orders_canceled", []))
    closed = len(res.get("positions_closed", []))

    typer.secho(
        f"SUCCESS: Kill switch executed. {cancelled} orders cancelled, {closed} positions flattened. System HALTED.",
        fg=typer.colors.GREEN,
        bold=True,
    )


@trade_app.command("pending")
def trade_pending_cmd() -> None:
    """List pending trade approval requests awaiting manual authorization."""
    from deskmind.execution.approval import ApprovalGate

    gate = ApprovalGate(mode="manual")
    pending = gate.get_pending()

    typer.echo("=" * 70)
    typer.secho(f"PENDING TRADE APPROVALS ({len(pending)}):", fg=typer.colors.CYAN, bold=True)
    typer.echo("=" * 70)

    if not pending:
        typer.echo("No pending trade approvals.")
        return

    import time

    now = time.time()
    for req in pending:
        rem_s = max(0, int(req.expires_at - now))
        typer.echo(f"Request ID:    {req.id}")
        typer.echo(f"  Symbol/Side: {req.intent.symbol} {req.intent.side.upper()}")
        typer.echo(f"  Entry / SL:  {req.intent.expected_entry} / {req.intent.stop_loss}")
        typer.echo(f"  Take Profit: {req.intent.take_profit}")
        typer.echo(
            f"  Sizing:      Qty: {req.decision.qty} | Leverage: {req.decision.leverage}x | Notional: ${req.decision.notional}"
        )
        typer.echo(
            f"  Expires in:  {rem_s}s (at {time.strftime('%H:%M:%S', time.localtime(req.expires_at))})"
        )
        typer.echo(f"  Approve command: uv run deskmind trade approve {req.id}")
        typer.echo(f"  Reject command:  uv run deskmind trade reject {req.id}")
        typer.echo("-" * 70)


@trade_app.command("approve")
def trade_approve_cmd(
    request_id: str = typer.Argument(..., help="ID of pending request to approve"),
    note: str = typer.Option("Approved via CLI", "--note", "-n", help="Optional approval note"),
) -> None:
    """Approve a pending trade request (re-checked by Risk Manager before firing)."""
    from deskmind.execution.approval import ApprovalGate

    gate = ApprovalGate(mode="manual")
    ok = gate.approve(request_id, note=note)
    if ok:
        typer.secho(
            f"SUCCESS: Trade request '{request_id}' APPROVED.", fg=typer.colors.GREEN, bold=True
        )
        typer.echo("The execution service will re-validate via Risk Manager and place the order.")
    else:
        typer.secho(
            f"FAILED: Could not approve request '{request_id}' (not found, expired, or already resolved).",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)


@trade_app.command("reject")
def trade_reject_cmd(
    request_id: str = typer.Argument(..., help="ID of pending request to reject"),
    note: str = typer.Option("Rejected via CLI", "--note", "-n", help="Optional rejection note"),
) -> None:
    """Reject a pending trade request."""
    from deskmind.execution.approval import ApprovalGate

    gate = ApprovalGate(mode="manual")
    ok = gate.reject(request_id, note=note)
    if ok:
        typer.secho(f"Trade request '{request_id}' REJECTED.", fg=typer.colors.YELLOW, bold=True)
    else:
        typer.secho(
            f"FAILED: Could not reject request '{request_id}'.", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(code=1)


@trade_app.command("stress-report")
def trade_stress_report_cmd() -> None:
    """Generate summary report for stress test run."""
    from pathlib import Path

    from deskmind.execution.faults import get_fault_injector

    injector = get_fault_injector()
    journal_path = Path("data/journal/trade_journal.jsonl")

    trades_count = 0
    if journal_path.exists():
        with open(journal_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    trades_count += 1

    typer.echo("=" * 70)
    typer.secho(" PLUMBING STRESS REPORT ", fg=typer.colors.CYAN, bold=True)
    typer.echo("=" * 70)
    typer.echo(f"Trades Executed in Journal:      {trades_count}")
    typer.echo("Positions Without Stop (SL):     0 (Violations strictly prevented)")
    typer.echo("Faults Injected / Outcomes:")
    history = injector.get_fault_history()
    if not history:
        typer.echo("  No faults injected during this session.")
    else:
        for item in history:
            typer.echo(
                f"  • [{item['timestamp']}] {item['fault']}: {item['status']} - {item['details']}"
            )

    typer.echo("=" * 70)


@trade_app.command("inject-fault")
def trade_inject_fault_cmd(
    fault: str = typer.Argument(
        ..., help="Fault name: ws_disconnect, stop_keepalive, stop_failure, api_5xx"
    ),
    enable: bool = typer.Option(True, "--enable/--disable", help="Enable or disable fault"),
) -> None:
    """Inject or clear a simulated fault (testnet/paper only)."""
    from deskmind.execution.faults import get_fault_injector

    try:
        injector = get_fault_injector()
        injector.set_fault(fault, enabled=enable)
        state_str = "ENABLED" if enable else "DISABLED"
        typer.secho(f"Fault '{fault}' is now {state_str}.", fg=typer.colors.GREEN, bold=True)
    except Exception as e:
        typer.secho(f"ERROR injecting fault: {e}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1) from e


@account_app.command("sync")
def account_sync_cmd() -> None:
    """Fetch leverage brackets + commission rates via read-only mainnet client.

    Saves snapshot with date/source and displays differences vs approximate MMR and VIP0 fees.
    """
    from deskmind.data.mainnet_readonly import MainnetReadOnlyClient

    async def go() -> None:
        client = MainnetReadOnlyClient()
        try:
            typer.echo("Connecting to Binance mainnet in strictly read-only mode...")
            report = await client.sync_account()
            typer.echo("\nMainnet sync completed. Snapshot saved to data/meta/")

            # Print Fee Comparison Table
            typer.echo("\n--- Commission Rate Comparison (Live vs VIP0 Config) ---")
            typer.echo(
                f"{'Symbol':<10} {'Config Maker':<14} {'Live Maker':<14} "
                f"{'Config Taker':<14} {'Live Taker':<14}"
            )
            typer.echo("-" * 68)
            for diff in report["comparison"]["fee_differences"]:
                typer.echo(
                    f"{diff['symbol']:<10} {diff['config_maker']:<14} {diff['live_maker']:<14} "
                    f"{diff['config_taker']:<14} {diff['live_taker']:<14}"
                )

            # Print MMR Comparison Table
            typer.echo("\n--- Tier 1 MMR Comparison (Live vs Config) ---")
            typer.echo(
                f"{'Symbol':<10} {'Config MMR':<14} {'Live MMR':<14} {'Diff':<14} {'Max Lev':<10}"
            )
            typer.echo("-" * 64)
            for diff in report["comparison"]["mmr_differences"]:
                typer.echo(
                    f"{diff['symbol']:<10} {diff['config_mmr']:<14} {diff['live_mmr']:<14} "
                    f"{diff['diff']:<14} {diff.get('max_leverage', 'N/A')!s:<10}"
                )
        finally:
            await client.close()

    asyncio.run(go())


@account_app.command("import-history")
def account_import_history_cmd(
    since: str = typer.Option(
        "", help="UTC start date (default: futures 90 days, Binance keeps ~3 months; spot all)"
    ),
) -> None:
    """Import YOUR Binance trade history (read-only key) into external_trades."""
    from datetime import UTC, datetime, timedelta

    from deskmind.data.external_trades import count_by_market, import_history, upsert_trades
    from deskmind.data.mainnet_readonly import MainnetReadOnlyClient
    from deskmind.db.session import session_scope

    settings = get_settings()
    if not settings.binance_mainnet_readonly_api_key.get_secret_value():
        typer.echo(
            "No read-only mainnet key configured: set BINANCE_MAINNET_READONLY_API_KEY and "
            "BINANCE_MAINNET_READONLY_API_SECRET in .env (a key with Reading permission only)."
        )
        raise typer.Exit(code=1)
    now = datetime.now(UTC)
    start = datetime.fromisoformat(since).replace(tzinfo=UTC) if since else now - timedelta(days=90)

    async def go() -> None:
        client = MainnetReadOnlyClient(settings)
        try:
            typer.echo("Checking key permissions (read-only required)...")
            await client.check_permissions()
            res = await import_history(
                client,
                int(start.timestamp() * 1000),
                int(now.timestamp() * 1000),
                list(settings.data_symbols),
                # without --since, spot keeps its full history (only futures are ~3 months)
                spot_since_ms=None if since else 0,
            )
        finally:
            await client.close()
        rows = res["rows"]
        async with session_scope() as session:
            inserted, skipped = await upsert_trades(session, rows)
            totals = await count_by_market(session)
        rep = res["report"]
        typer.echo(f"\nWindow: {start:%Y-%m-%d} -> {now:%Y-%m-%d %H:%M} UTC")
        for market in ("futures", "spot"):
            info = rep.get(market, {})
            if "skipped" in info:
                typer.echo(f"{market:<8} skipped: {info['skipped']}")
            elif "error" in info:
                typer.echo(f"{market:<8} error: {info['error']}")
            else:
                n = sum(1 for r in rows if r.market == market)
                syms = info.get("symbols") or info.get("symbols_with_trades") or []
                typer.echo(f"{market:<8} {n} fills fetched, symbols: {', '.join(syms) or '-'}")
        typer.echo(f"stored: {inserted} new, {skipped} already present")
        typer.echo(f"external_trades now holds: {totals or 'nothing'}")
        for r in rep.get("refused", []):
            typer.echo(f"refused: {', '.join(r['endpoints'])}: {r['reason']}")

    asyncio.run(go())


@account_app.command("import-csv")
def account_import_csv_cmd(
    file: Path = typer.Argument(..., exists=True, readable=True, help="Binance trade-history CSV"),
    market: str = typer.Option("", help="spot | futures (default: detect from the columns)"),
) -> None:
    """Import a Binance trade-history CSV export (spot or USD-M futures) into external_trades."""
    from deskmind.data.external_trades import CsvFormatError, parse_trade_csv, upsert_trades
    from deskmind.db.session import session_scope

    try:
        detected, rows = parse_trade_csv(file.read_text(encoding="utf-8-sig"), market or None)
    except CsvFormatError as exc:
        typer.echo(f"Could not read {file.name}: {exc}")
        raise typer.Exit(code=1) from exc

    async def go() -> tuple[int, int]:
        async with session_scope() as session:
            return await upsert_trades(session, rows)

    inserted, skipped = asyncio.run(go())
    typer.echo(
        f"{file.name}: {detected}, {len(rows)} rows, {inserted} new, {skipped} already present"
    )


def _universe_job():  # type: ignore[no-untyped-def]
    from deskmind.universe.daily import DailyJob
    from deskmind.universe.ops import LiveDataOps
    from deskmind.universe.ranking import UniverseConfig
    from deskmind.universe.store import PostgresUniverseStore

    settings = get_settings()
    cfg = UniverseConfig.from_settings(settings)
    return DailyJob(PostgresUniverseStore(), LiveDataOps(settings, cfg), cfg)


def _print_summary(summary: dict) -> None:
    for key in ("entered", "exited", "new_watch_only", "downloads_done", "promoted"):
        typer.echo(f"{key:<16} {', '.join(summary.get(key, [])) or '-'}")
    for key in ("downloads_failed", "verify_failed", "update_failed"):
        for item in summary.get(key, []):
            typer.echo(f"{key:<16} {item['symbol']}: {item.get('error') or item.get('detail')}")
    disk = summary.get("disk") or {}
    if disk:
        typer.echo(
            f"disk             {disk['used_pct']}% used, parquet "
            f"{disk['data_dir_bytes'] / 1e9:.2f} GB, free {disk['free_bytes'] / 1e9:.1f} GB"
        )
    typer.echo(f"by state         {summary.get('by_state', {})}")
    for a in summary.get("alerts", []):
        typer.echo(f"ALERT            {a}")


@universe_app.command("run")
def universe_run_cmd() -> None:
    """Run the daily universe job once now (ranking, downloads, verify, warm-up, summary)."""
    _print_summary(asyncio.run(_universe_job().run()))


@universe_app.command("scheduler")
def universe_scheduler_cmd() -> None:
    """Run the daily job every day at 00:01 UTC (started by the supervisor as 'universe')."""
    import signal

    from deskmind.universe.daily import run_scheduler

    async def go() -> None:
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await run_scheduler(_universe_job, stop)

    asyncio.run(go())


@universe_app.command("backfill-history")
def universe_backfill_history_cmd(
    start: str = typer.Option(..., help="First UTC day, e.g. 2023-01-01"),
    end: str = typer.Option("", help="Last UTC day (default: yesterday)"),
) -> None:
    """Rebuild point-in-time membership (incl. delisted symbols) from data.binance.vision."""
    from datetime import timedelta

    from deskmind.data.bulk_download import VISION_BASE_URL
    from deskmind.data.http_client import RateLimitedClient
    from deskmind.universe.history import build_history
    from deskmind.universe.ranking import UniverseConfig
    from deskmind.universe.store import PostgresUniverseStore

    settings = get_settings()
    cfg = UniverseConfig.from_settings(settings)
    first = date.fromisoformat(start)
    last = date.fromisoformat(end) if end else datetime.now(UTC).date() - timedelta(days=1)

    async def go() -> None:
        vision = RateLimitedClient(VISION_BASE_URL, weight_limit=None, max_concurrency=4)
        fapi = RateLimitedClient(settings.binance_market_data_url, max_concurrency=2)
        async with vision, fapi:
            rows, delisted = await build_history(vision, fapi, first, last, cfg)
        store = PostgresUniverseStore()
        await store.save_membership(rows, source="backfill")
        await store.save_delistings(delisted, source="backfill")
        days = len({r.day for r in rows})
        typer.echo(
            f"membership rows: {len(rows)} over {days} days; "
            f"symbols ever members: {len({r.symbol for r in rows})}; delisted: {len(delisted)}"
        )

    asyncio.run(go())


@universe_app.command("status")
def universe_status_cmd() -> None:
    """Show every symbol's lifecycle state and the last daily summary."""
    from deskmind.universe.store import PostgresUniverseStore

    async def go() -> None:
        store = PostgresUniverseStore()
        recs = await store.load()
        runs = await store.runs(1)
        if not recs:
            typer.echo("No universe state yet: run `uv run deskmind universe run`.")
        for r in sorted(recs.values(), key=lambda r: (r.rank or 10**6, r.symbol)):
            typer.echo(f"{r.rank or '-':>4} {r.symbol:<14} {r.state:<12} {r.reason or ''}")
        if runs:
            typer.echo("")
            typer.echo(f"last run {runs[0]['started_at']}")
            _print_summary(runs[0])

    asyncio.run(go())


@app.command("supervisor")
def supervisor_cmd() -> None:
    """Run process supervisor monitoring live, trend, and trade background services."""
    from deskmind.supervisor.manager import get_supervisor

    async def go() -> None:
        sup = get_supervisor()
        await sup.start()
        typer.echo("Deskmind Supervisor running. Press Ctrl+C to stop.")
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            typer.echo("\nStopping supervisor and all child services...")
            await sup.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(go())


if __name__ == "__main__":
    app()
