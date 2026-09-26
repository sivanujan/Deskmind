import pyarrow as pa

from deskmind.data.datasets import FUNDING, HOUR_MS, MINUTE_MS, Dataset
from deskmind.data.storage import (
    candles_table,
    create_view_from_arrow,
    duckdb_connect,
    funding_table,
    merge_tables,
)
from deskmind.data.verify import FAIL, PASS, WARN, check_candles, check_cross_tf, check_funding
from tests.data_fixtures import kline_row, resample_rows

M5 = Dataset("klines", "5m")
M15 = Dataset("klines", "15m")
IV = 5 * MINUTE_MS
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
N = 288  # one day of 5m candles
NOW = T0 + 2 * 24 * HOUR_MS  # well after the data


def rows_5m(n: int = N) -> list[list[str]]:
    return [kline_row(T0 + i * IV, IV) for i in range(n)]


def run(rows: list[list[str]], *, now: int = NOW, raw: pa.Table | None = None, **kw):
    con = duckdb_connect()
    create_view_from_arrow(con, "v", raw if raw is not None else candles_table(M5, rows))
    return check_candles(con, "v", M5, start_ms=T0, now_ms=now, **kw)


def status_of(rep, name: str) -> str:
    return next(c.status for c in rep.checks if c.name == name)


def test_clean_data_passes() -> None:
    rep = run(rows_5m())
    assert rep.rows == N
    assert rep.gaps == []
    assert rep.status == WARN  # only "up to last closed candle": data ends a day before NOW
    assert all(c.status == PASS for c in rep.checks if c.name != "up to last closed candle")


def test_gap_detection_lists_start_end_and_count() -> None:
    rows = rows_5m()
    del rows[100:103]  # 3 missing candles
    del rows[200:201]  # original candle 203 (16:55) after the first deletion
    rep = run(rows, complete_months={"2024-01"})
    assert [
        (g.missing, g.first_missing.isoformat(), g.last_missing.isoformat()) for g in rep.gaps
    ] == [
        (3, "2024-01-01T08:20:00+00:00", "2024-01-01T08:30:00+00:00"),
        (1, "2024-01-01T16:55:00+00:00", "2024-01-01T16:55:00+00:00"),
    ]
    assert rep.missing_total == 4
    assert status_of(rep, "no gaps") == WARN
    assert "source gap" in rep.gaps[0].note


def test_leading_gap_before_first_candle() -> None:
    rep = run(rows_5m()[12:])
    assert rep.gaps[0].missing == 12
    assert rep.gaps[0].first_missing.isoformat() == "2024-01-01T00:00:00+00:00"


def test_duplicates_fail() -> None:
    rows = rows_5m()
    rows.insert(50, rows[50])
    rep = run(rows)
    assert status_of(rep, "no duplicate open_time") == FAIL
    assert rep.examples["duplicates"]


def test_non_monotonic_storage_order_fails() -> None:
    rows = rows_5m()
    rows[10], rows[11] = rows[11], rows[10]
    rep = run(rows)
    assert status_of(rep, "monotonic increasing time") == FAIL
    assert rep.gaps == []  # gap detection is order-independent


def test_ohlc_sanity_violations_fail() -> None:
    rows = rows_5m()
    rows[5][2] = str(float(rows[5][1]) - 10)  # high below open
    rows[6][3] = "0"  # zero low
    rows[7][5] = "-1"  # negative volume
    rep = run(rows)
    assert status_of(rep, "OHLC sanity") == FAIL
    assert len(rep.examples["ohlc_violations"]) == 3


def test_unclosed_candle_fails() -> None:
    rows = rows_5m()
    last_open = int(rows[-1][0])
    rep = run(rows, now=last_open + IV - 1)  # last candle still forming
    assert status_of(rep, "closed candles only") == FAIL
    rep_ok = run(rows, now=last_open + IV)
    assert status_of(rep_ok, "closed candles only") == PASS


def cross(rows5: list[list[str]], rows15: list[list[str]]):
    con = duckdb_connect()
    create_view_from_arrow(con, "s", candles_table(M5, rows5))
    create_view_from_arrow(con, "d", candles_table(M15, rows15))
    return check_cross_tf(con, "s", "d", M5, M15)


def test_resample_consistency_pass() -> None:
    rows5 = rows_5m()
    rep = cross(rows5, resample_rows(rows5, 3, IV))
    assert rep.compared == N // 3
    assert rep.mismatches == 0 and rep.volume_mismatches == 0
    assert rep.status == PASS


def test_resample_mismatch_detected() -> None:
    rows5 = rows_5m()
    rows15 = resample_rows(rows5, 3, IV)
    rows15[4][2] = str(float(rows15[4][2]) + 0.5)  # wrong high
    rows15[9][4] = str(float(rows15[9][4]) - 0.1)  # wrong close
    rep = cross(rows5, rows15)
    assert rep.mismatches == 2
    assert rep.status == FAIL
    assert len(rep.examples) == 2


def test_resample_skips_incomplete_buckets_and_flags_missing_target() -> None:
    rows5 = rows_5m()
    rows15 = resample_rows(rows5, 3, IV)
    del rows5[31]  # bucket 10 incomplete -> skipped, not a mismatch
    del rows15[20]  # complete bucket missing in target
    rep = cross(rows5, rows15)
    assert rep.skipped_incomplete == 1
    assert rep.missing_in_target == 1
    assert rep.mismatches == 0


def funding_rep(times: list[int], now: int):
    con = duckdb_connect()
    create_view_from_arrow(con, "f", funding_table(times, [0.0001] * len(times)))
    return check_funding(con, "f", start_ms=T0, now_ms=now)


def test_funding_regular_passes() -> None:
    times = [T0 + i * 8 * HOUR_MS for i in range(30)]
    rep = funding_rep(times, now=times[-1] + HOUR_MS)
    assert rep.status == PASS, rep.checks


def test_funding_irregular_interval_and_duplicates() -> None:
    times = [T0 + i * 8 * HOUR_MS for i in range(30)]
    del times[10]  # one missing event -> 16h interval
    times.append(times[-1])  # duplicate
    rep = funding_rep(times, now=times[-1] + HOUR_MS)
    assert next(c for c in rep.checks if c.name == "interval regularity").status == WARN
    assert next(c for c in rep.checks if c.name == "no duplicate funding_time").status == FAIL
    assert len(rep.gaps) == 1 and rep.gaps[0].missing == 1


def test_merge_is_idempotent_and_dedupes() -> None:
    a = candles_table(M5, rows_5m(10))
    b = candles_table(M5, rows_5m(15)[5:])
    once = merge_tables(M5, a, b)
    twice = merge_tables(M5, once, a, b)
    assert once.num_rows == 15
    assert twice.equals(once)
    f = funding_table([T0, T0 + 3], [0.1, 0.2])  # ms jitter -> same event
    assert merge_tables(FUNDING, f).num_rows == 1
