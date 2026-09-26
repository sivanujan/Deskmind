"""Config-driven symbol list, CLI symbol resolution, multi-symbol verify features (offline)."""

from datetime import UTC, datetime

import pytest
import typer
from pydantic import ValidationError

from deskmind.cli import resolve_symbols
from deskmind.config import DEFAULT_DATA_SYMBOLS, Settings
from deskmind.data.datasets import HOUR_MS, MINUTE_MS, Dataset
from deskmind.data.storage import (
    Store,
    candles_table,
    create_view_from_arrow,
    duckdb_connect,
    funding_table,
    load_klines,
    write_parquet,
)
from deskmind.data.verify import DatasetReport as DR
from deskmind.data.verify import (
    Gap,
    VerifyReport,
    annotate_cross_symbol,
    check_funding,
    check_mark_vs_last,
    render_run_markdown,
)
from tests.data_fixtures import kline_row

T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
IV = 5 * MINUTE_MS
M5, MK5 = Dataset("klines", "5m"), Dataset("mark_klines", "5m")


def settings(**kw) -> Settings:
    return Settings(_env_file=None, postgres_password="x", **kw)


# ---------------------------------------------------------------- config


def test_default_symbols_come_from_config() -> None:
    assert settings().data_symbols == list(DEFAULT_DATA_SYMBOLS)
    assert DEFAULT_DATA_SYMBOLS == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


def test_symbols_from_env_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_SYMBOLS", " btcusdt, DOGEUSDT ,BTCUSDT,,")
    assert Settings(_env_file=None).data_symbols == ["BTCUSDT", "DOGEUSDT"]


@pytest.mark.parametrize("bad", ["", "BTC-USDT", "BTCUSD", "ETHBTC"])
def test_invalid_symbols_rejected(bad: str) -> None:
    with pytest.raises(ValidationError, match="DATA_SYMBOLS"):
        settings(data_symbols=bad)


def test_resolve_symbols() -> None:
    s = settings(data_symbols="BTCUSDT,ETHUSDT")
    assert resolve_symbols(s, None, True) == ["BTCUSDT", "ETHUSDT"]
    assert resolve_symbols(s, "ethusdt", False) == ["ETHUSDT"]
    with pytest.raises(typer.BadParameter, match="not in DATA_SYMBOLS"):
        resolve_symbols(s, "SOLUSDT", False)  # must be added to config first
    with pytest.raises(typer.BadParameter, match="exactly one"):
        resolve_symbols(s, None, False)
    with pytest.raises(typer.BadParameter, match="exactly one"):
        resolve_symbols(s, "BTCUSDT", True)


def test_no_symbol_hardcoded_in_source() -> None:
    """Symbols live only in config.py (DEFAULT_DATA_SYMBOLS); code takes `symbol` params."""
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "deskmind"
    offenders = [
        f"{p.relative_to(src)}:{i}"
        for p in src.rglob("*.py")
        if p.name != "config.py"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if any(sym in line for sym in DEFAULT_DATA_SYMBOLS)
        and not line.lstrip().startswith("#")
        and '"""' not in line
        and "e.g." not in line
    ]
    assert offenders == []


# ---------------------------------------------------------------- storage


def test_load_klines_range_and_order(tmp_path) -> None:
    store = Store(tmp_path, "ETHUSDT")
    rows = [kline_row(T0 + i * IV, IV, 1500) for i in range(20)]
    write_parquet(store.partition_path(M5, 2024, 1), candles_table(M5, rows[10:]))
    write_parquet(store.partition_path(M5, 2023, 12), candles_table(M5, []))
    write_parquet(store.partition_path(M5, 2024, 1), candles_table(M5, rows[::-1]))
    start = datetime.fromtimestamp((T0 + 5 * IV) / 1000, UTC)
    end = datetime.fromtimestamp((T0 + 8 * IV) / 1000, UTC)
    tbl = load_klines(tmp_path, "ETHUSDT", "5m", start, end)
    times = tbl.column("open_time").cast("int64").to_pylist()
    assert times == [T0 + i * IV for i in (5, 6, 7)]
    assert load_klines(tmp_path, "SOLUSDT", "5m").num_rows == 0


# ---------------------------------------------------------------- mark vs last


def test_mark_vs_last_flags_abnormal_divergence_as_episodes() -> None:
    last = [kline_row(T0 + i * IV, IV) for i in range(50)]
    mark = [list(r) for r in last]
    for i in (10, 11, 12, 30):  # two episodes: 3 candles and 1 candle
        mark[i][4] = f"{float(last[i][4]) * (1.05 if i != 30 else 0.98):.2f}"
    mark[20][4] = f"{float(last[20][4]) * 1.002:.2f}"  # 0.2%: normal basis, not flagged
    con = duckdb_connect()
    create_view_from_arrow(con, "l", candles_table(M5, last))
    create_view_from_arrow(con, "m", candles_table(MK5, mark))
    rep = check_mark_vs_last(con, "l", "m", M5)
    assert rep.compared == 50 and rep.flagged == 4
    assert [(n, round(d, 3)) for _, _, n, d in rep.episodes] == [(3, 0.05), (1, 0.02)]
    assert rep.status == "INFO"
    assert 0.049 < rep.max_dev < 0.051


# ---------------------------------------------------------------- funding interval changes


def test_announced_funding_interval_change_is_not_irregular() -> None:
    times = [T0 + i * 8 * HOUR_MS for i in range(20)]
    times += [times[-1] + (i + 1) * 4 * HOUR_MS for i in range(6)]  # switched to 4h
    hours = [8] * 20 + [4] * 6
    con = duckdb_connect()
    create_view_from_arrow(
        con, "f", funding_table(times, [0.0001] * len(times), interval_hours=hours)
    )
    rep = check_funding(con, "f", start_ms=T0, now_ms=times[-1] + HOUR_MS)
    check = next(c for c in rep.checks if c.name == "interval regularity")
    assert check.status == "PASS", check.detail
    assert "6 match an announced interval change" in check.detail


# ---------------------------------------------------------------- cross-symbol gaps


def _report(symbol: str, gaps: list[tuple[int, int]]) -> VerifyReport:
    d = DR("klines/5m")
    for a, b in gaps:
        start = datetime.fromtimestamp(a / 1000, UTC)
        end = datetime.fromtimestamp(b / 1000, UTC)
        d.gaps.append(Gap(start, end, (b - a) // IV + 1))
    return VerifyReport(symbol, datetime.now(UTC), None, [d], [])


def test_cross_symbol_gap_annotation_and_summary() -> None:
    outage = (T0 + 10 * IV, T0 + 12 * IV)
    reps = [
        _report("BTCUSDT", [outage]),
        _report("ETHUSDT", [(T0 + 11 * IV, T0 + 11 * IV)]),  # overlaps the same outage
        _report("SOLUSDT", [(T0 + 100 * IV, T0 + 100 * IV)]),  # unrelated
    ]
    annotate_cross_symbol(reps)
    assert reps[0].datasets[0].gaps[0].also_in == ["ETHUSDT"]
    assert reps[1].datasets[0].gaps[0].also_in == ["BTCUSDT"]
    assert reps[2].datasets[0].gaps[0].also_in == []
    md = render_run_markdown(reps)
    assert "| BTCUSDT |" in md and "| SOLUSDT |" in md
    assert "exchange-wide" in md and "this symbol only" in md
