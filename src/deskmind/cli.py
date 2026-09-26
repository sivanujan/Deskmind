"""Deskmind command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date

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
AGENTS = ("trend",)


def _check_agent(name: str) -> None:
    if name not in AGENTS:
        raise typer.BadParameter(f"unknown agent {name!r}; available: {', '.join(AGENTS)}")


@agents_app.command("run")
def agents_run(
    agent: str = typer.Argument(..., help="Agent name (trend)"),
    duration: float = typer.Option(0, help="Stop after N seconds (0 = until Ctrl+C)"),
) -> None:
    """Run an agent live: warm up, then process every deskmind:bar_close event."""
    _check_agent(agent)
    from deskmind.agents.trend.runner import run_live

    asyncio.run(run_live(get_settings(), duration or None))


@agents_app.command("backfill")
def agents_backfill(
    agent: str = typer.Argument(..., help="Agent name (trend)"),
    symbol: str | None = SymbolOpt,
    all_symbols: bool = AllOpt,
    start: str = typer.Option(..., help="UTC start, e.g. 2026-06-28"),
    end: str = typer.Option("", help="UTC end (default: now)"),
) -> None:
    """Replay history through the agent and store reports + overlays for [start, end]."""
    _check_agent(agent)
    import time
    from datetime import UTC, datetime

    from deskmind.agents.trend.runner import backfill, parse_day
    from deskmind.agents.trend.store import PostgresTrendStore
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
                r = await backfill(
                    sym,
                    list(settings.live_timeframes),
                    start_ms,
                    end_ms,
                    data_dir=settings.data_dir,
                    recent=PostgresRecentCandles(engine),
                    store=PostgresTrendStore(engine),
                )
                typer.echo(
                    f"\n== {sym}: {r['reports']} reports, {r['items']} items "
                    f"({time.time() - t0:.1f}s)"
                )
                kinds = ["swing_high", "swing_low", "bos", "choch", "sweep",
                         "trendline_support", "trendline_resistance"]  # fmt: skip
                typer.echo(f"{'tf':<5}" + "".join(f"{k:>22}" for k in kinds))
                for tf in settings.live_timeframes:
                    c = r["counts"].get(tf, {})
                    typer.echo(f"{tf:<5}" + "".join(f"{c.get(k, 0):>22}" for k in kinds))
        finally:
            await engine.dispose()

    asyncio.run(go())


@agents_app.command("check")
def agents_check(
    agent: str = typer.Argument(..., help="Agent name (trend)"),
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
    from deskmind.agents.trend.checks import convergence_check, truncation_check

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


if __name__ == "__main__":
    app()
