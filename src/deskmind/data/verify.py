"""Data-quality verification for stored historical data (DuckDB over Parquet).

Checks per candle dataset: duplicates, monotonic order, grid alignment, gaps (every one listed),
closed-candles-only, close_time consistency, OHLC sanity. Cross-timeframe: 5m resampled to
15m/1h/2h must equal the downloaded candles. Mark vs last price: abnormal divergence is listed
(report only). Funding: duplicates, order, interval regularity (announced interval changes are not
irregular). Multi-symbol runs cross-reference gaps: a gap seen on several symbols at the same time
is exchange-wide.

Gaps are reported, never filled: remaining gaps were absent from both bulk files and REST.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from deskmind.data.datasets import (
    CROSS_TF_CHECKS,
    DEFAULT_DATASETS,
    HOUR_MS,
    MARK_LAST_CHECKS,
    Dataset,
    date_ms,
    from_ms,
    last_closed_open_ms,
    to_ms,
)
from deskmind.data.storage import Store, create_view, duckdb_connect

PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"
_RANK = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2}  # INFO is report-only: never changes the status
MAX_LISTED = 200
MARK_LAST_THRESHOLD = 0.01  # |mark - last| / last on candle close; 1% is abnormal for majors


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Gap:
    first_missing: datetime
    last_missing: datetime
    missing: int
    note: str = ""
    also_in: list[str] = field(default_factory=list)  # other symbols with an overlapping gap


@dataclass
class DatasetReport:
    key: str
    rows: int = 0
    first: datetime | None = None
    last: datetime | None = None
    checks: list[Check] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    examples: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    rest_replaced: int = 0  # bulk rows replaced by REST data during reconcile

    @property
    def status(self) -> str:
        return max((c.status for c in self.checks), key=_RANK.__getitem__, default=PASS)

    @property
    def missing_total(self) -> int:
        return sum(g.missing for g in self.gaps)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))


@dataclass
class CrossTfReport:
    source: str
    target: str
    compared: int = 0
    mismatches: int = 0
    skipped_incomplete: int = 0
    missing_in_target: int = 0
    volume_mismatches: int = 0
    confirmed_source: int = 0  # mismatches REST re-download confirmed as exchange-side
    mismatch_buckets: list[int] = field(default_factory=list)
    confirmed_buckets: list[int] = field(default_factory=list)
    volume_mismatch_buckets: list[int] = field(default_factory=list)
    examples: list[tuple[Any, ...]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.mismatches:
            return FAIL
        if (
            self.missing_in_target
            or self.volume_mismatches
            or self.confirmed_source
            or self.compared == 0
        ):
            return WARN
        return PASS


@dataclass
class MarkLastReport:
    """Mark vs last price divergence on candle close. Report only (status INFO)."""

    timeframe: str
    threshold: float = MARK_LAST_THRESHOLD
    compared: int = 0
    flagged: int = 0
    median_dev: float = 0.0
    p99_dev: float = 0.0
    max_dev: float = 0.0
    episodes: list[tuple[datetime, datetime, int, float]] = field(default_factory=list)
    status: str = INFO


@dataclass
class VerifyReport:
    symbol: str
    generated_at: datetime
    start: date | None
    datasets: list[DatasetReport]
    cross_tf: list[CrossTfReport]
    mark_last: list[MarkLastReport] = field(default_factory=list)

    @property
    def status(self) -> str:
        statuses = [d.status for d in self.datasets] + [c.status for c in self.cross_tf]
        return max(statuses, key=_RANK.__getitem__, default=PASS)


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[Any, ...]:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------- candles


def check_candles(
    con: duckdb.DuckDBPyConnection,
    view: str,
    ds: Dataset,
    *,
    start_ms: int | None,
    now_ms: int,
    complete_months: set[str] | None = None,
    rest_checked: set[int] | None = None,
    replaced: int = 0,
) -> DatasetReport:
    rep = DatasetReport(ds.key, rest_replaced=replaced)
    iv = ds.interval_ms
    rows, first, last = _one(
        con, f"SELECT count(*), epoch_ms(min(open_time)), epoch_ms(max(open_time)) FROM {view}"
    )
    rep.rows = rows
    if rows == 0:
        rep.add("data present", FAIL, "no rows")
        return rep
    rep.first, rep.last = from_ms(first), from_ms(last)

    dups = con.execute(
        f"SELECT strftime(open_time, '%Y-%m-%d %H:%M') t, count(*) c FROM {view} "
        "GROUP BY 1 HAVING c > 1 ORDER BY 1 LIMIT 20"
    ).fetchall()
    n_dup = _one(con, f"SELECT count(*) - count(DISTINCT open_time) FROM {view}")[0]
    rep.add("no duplicate open_time", FAIL if n_dup else PASS, f"{n_dup} duplicate rows")
    if dups:
        rep.examples["duplicates"] = dups

    n_nonmono = _one(
        con,
        f"""SELECT count(*) FROM (
              SELECT open_time, lag(open_time) OVER (ORDER BY _ord) AS prev FROM {view})
            WHERE prev IS NOT NULL AND open_time <= prev""",
    )[0]
    rep.add(
        "monotonic increasing time",
        FAIL if n_nonmono else PASS,
        f"{n_nonmono} rows not after their predecessor (storage order)",
    )

    n_misaligned = _one(con, f"SELECT count(*) FROM {view} WHERE epoch_ms(open_time) % {iv} <> 0")[
        0
    ]
    rep.add(
        "open_time on interval grid",
        FAIL if n_misaligned else PASS,
        f"{n_misaligned} misaligned rows",
    )

    bad_close = _one(
        con,
        f"SELECT count(*) FROM {view} WHERE epoch_ms(close_time) <> epoch_ms(open_time) + {iv} - 1",
    )[0]
    rep.add("close_time = open_time + interval - 1ms", WARN if bad_close else PASS, f"{bad_close}")

    last_closed_bound = last_closed_open_ms(now_ms, iv)
    n_open = _one(
        con, f"SELECT count(*) FROM {view} WHERE epoch_ms(open_time) >= {last_closed_bound}"
    )[0]
    rep.add("closed candles only", FAIL if n_open else PASS, f"{n_open} unclosed/future candles")

    vol_cond = " OR volume < 0 OR isnan(volume)" if ds.kind == "klines" else ""
    bad_ohlc_where = f"""
        NOT (open > 0 AND high > 0 AND low > 0 AND close > 0
             AND high >= greatest(open, close) AND low <= least(open, close) AND high >= low)
        OR isnan(open) OR isnan(high) OR isnan(low) OR isnan(close){vol_cond}"""
    n_bad = _one(con, f"SELECT count(*) FROM {view} WHERE {bad_ohlc_where}")[0]
    rep.add("OHLC sanity", FAIL if n_bad else PASS, f"{n_bad} rows violate OHLC/volume rules")
    if n_bad:
        cols = "strftime(open_time, '%Y-%m-%d %H:%M'), open, high, low, close" + (
            ", volume" if ds.kind == "klines" else ""
        )
        rep.examples["ohlc_violations"] = con.execute(
            f"SELECT {cols} FROM {view} WHERE {bad_ohlc_where} ORDER BY open_time LIMIT 20"
        ).fetchall()

    if ds.kind == "klines":
        # Zero-trade candles are either genuine (trading halted / no fills) or bulk-archive
        # placeholders. Reconcile re-checks each against REST; unchecked ones are a WARN.
        zero = [
            r[0]
            for r in con.execute(
                f"SELECT epoch_ms(open_time) FROM {view} WHERE trades = 0 ORDER BY 1"
            ).fetchall()
        ]
        unchecked = [t for t in zero if t not in (rest_checked or set())]
        rep.add(
            "zero-trade candles verified via REST",
            WARN if unchecked else PASS,
            f"{len(zero)} zero-trade candles, {len(zero) - len(unchecked)} confirmed by REST, "
            f"{len(unchecked)} unchecked; {replaced} bulk rows replaced by REST data",
        )

    # Gaps: consecutive distinct open_times further apart than one interval.
    gap_rows = con.execute(
        f"""WITH d AS (SELECT DISTINCT epoch_ms(open_time) AS t FROM {view}),
                 g AS (SELECT lag(t) OVER (ORDER BY t) AS prev, t FROM d)
            SELECT prev, t FROM g WHERE prev IS NOT NULL AND t - prev > {iv} ORDER BY prev"""
    ).fetchall()
    if start_ms is not None and first > start_ms:
        gap_rows.insert(0, (start_ms - iv, first))
    complete_months = complete_months or set()
    for prev, nxt in gap_rows:
        a, b = prev + iv, nxt - iv
        month = from_ms(a).strftime("%Y-%m")
        note = (
            "absent from bulk + REST (source gap)"
            if month in complete_months
            else "month not finalised yet"
        )
        rep.gaps.append(Gap(from_ms(a), from_ms(b), (nxt - prev) // iv - 1, note))
    missing = rep.missing_total
    rep.add(
        "no gaps",
        WARN if rep.gaps else PASS,
        f"{len(rep.gaps)} gaps, {missing} missing candles"
        + (f" ({missing / (rows + missing):.4%} of expected)" if missing else ""),
    )

    behind = (last_closed_bound - iv - last) // iv
    rep.add(
        "up to last closed candle",
        PASS if behind <= 0 else WARN,
        "current" if behind <= 0 else f"{behind} closed candles not downloaded yet (run update)",
    )
    return rep


def check_cross_tf(
    con: duckdb.DuckDBPyConnection,
    src_view: str,
    dst_view: str,
    src: Dataset,
    dst: Dataset,
    confirmed: set[int] | None = None,
) -> CrossTfReport:
    """Resample src into dst's interval and compare with the downloaded dst candles.

    Binance builds higher-timeframe klines from traded candles only: a zero-trade 5m candle
    (flat, open = previous close) does not set the 15m open/high/low. The resample follows that
    rule. `confirmed` holds bucket open times whose mismatch REST re-downloads could not resolve
    (the exchange's own data disagrees); those are WARN, everything else is FAIL.
    """
    rep = CrossTfReport(src.key, dst.key)
    t = dst.interval_ms
    k = t // src.interval_ms
    with_vol = src.kind == "klines" and dst.kind == "klines"
    vol_sel = ", sum(volume) AS v" if with_vol else ""
    traded = "FILTER (WHERE trades > 0)" if src.kind == "klines" else ""

    def agg(fn: str, *args: str) -> str:
        expr = f"{fn}({', '.join(args)})"
        return f"coalesce({expr} {traded}, {expr})" if traded else expr

    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE _xtf AS
            WITH b AS (SELECT (epoch_ms(open_time) // {t}) * {t} AS bucket, * FROM {src_view}),
                 agg AS (SELECT bucket, {agg("arg_min", "open", "open_time")} AS o,
                                {agg("max", "high")} AS h, {agg("min", "low")} AS l,
                                {agg("arg_max", "close", "open_time")} AS c,
                                count(*) AS n {vol_sel}
                         FROM b GROUP BY bucket)
            SELECT agg.*, d.open AS d_open, d.high AS d_high, d.low AS d_low, d.close AS d_close
                   {", d.volume AS d_volume" if with_vol else ""},
                   d.open_time IS NOT NULL AS in_target
            FROM agg LEFT JOIN {dst_view} d ON epoch_ms(d.open_time) = agg.bucket"""
    )

    def differs(a: str, b: str, rel: float = 1e-9, abs_tol: float = 1e-9) -> str:
        return f"abs({a} - {b}) > {rel} * abs({b}) + {abs_tol}"

    mismatch = " OR ".join(
        differs(a, b)
        for a, b in (("o", "d_open"), ("h", "d_high"), ("l", "d_low"), ("c", "d_close"))
    )
    rep.compared = _one(con, f"SELECT count(*) FROM _xtf WHERE n = {k} AND in_target")[0]
    rep.skipped_incomplete = _one(con, f"SELECT count(*) FROM _xtf WHERE n < {k}")[0]
    rep.missing_in_target = _one(con, f"SELECT count(*) FROM _xtf WHERE n = {k} AND NOT in_target")[
        0
    ]
    confirmed = confirmed or set()
    all_buckets = [
        r[0]
        for r in con.execute(
            f"SELECT bucket FROM _xtf WHERE n = {k} AND in_target AND ({mismatch}) ORDER BY bucket"
        ).fetchall()
    ]
    rep.mismatch_buckets = [b for b in all_buckets if b not in confirmed]
    rep.mismatches = len(rep.mismatch_buckets)
    rep.confirmed_buckets = [b for b in all_buckets if b in confirmed]
    rep.confirmed_source = len(rep.confirmed_buckets)
    if all_buckets:
        rep.examples = con.execute(
            f"""SELECT strftime(make_timestamptz(bucket * 1000), '%Y-%m-%d %H:%M'),
                       o, d_open, h, d_high, l, d_low, c, d_close
                FROM _xtf WHERE n = {k} AND in_target AND ({mismatch}) ORDER BY bucket LIMIT 20"""
        ).fetchall()
    if with_vol:
        rep.volume_mismatch_buckets = [
            r[0]
            for r in con.execute(
                f"SELECT bucket FROM _xtf WHERE n = {k} AND in_target AND "
                f"({differs('v', 'd_volume', 1e-6, 1e-6)}) ORDER BY bucket"
            ).fetchall()
            if r[0] not in confirmed
        ]
        rep.volume_mismatches = len(rep.volume_mismatch_buckets)
    return rep


# ---------------------------------------------------------------- funding


def check_funding(
    con: duckdb.DuckDBPyConnection, view: str, *, start_ms: int | None, now_ms: int
) -> DatasetReport:
    rep = DatasetReport("funding")
    rows, first, last = _one(
        con,
        f"SELECT count(*), epoch_ms(min(funding_time)), epoch_ms(max(funding_time)) FROM {view}",
    )
    rep.rows = rows
    if rows == 0:
        rep.add("data present", FAIL, "no rows")
        return rep
    rep.first, rep.last = from_ms(first), from_ms(last)

    n_dup = _one(con, f"SELECT count(*) - count(DISTINCT funding_time) FROM {view}")[0]
    rep.add("no duplicate funding_time", FAIL if n_dup else PASS, f"{n_dup} duplicate rows")

    n_nonmono = _one(
        con,
        f"""SELECT count(*) FROM (
              SELECT funding_time, lag(funding_time) OVER (ORDER BY _ord) AS prev FROM {view})
            WHERE prev IS NOT NULL AND funding_time <= prev""",
    )[0]
    rep.add("monotonic increasing time", FAIL if n_nonmono else PASS, f"{n_nonmono} rows")

    n_nan = _one(
        con, f"SELECT count(*) FROM {view} WHERE funding_rate IS NULL OR isnan(funding_rate)"
    )[0]
    rep.add("funding_rate present", FAIL if n_nan else PASS, f"{n_nan} null/NaN rates")

    lo, hi = _one(con, f"SELECT min(funding_rate), max(funding_rate) FROM {view}")
    rep.add("funding_rate range", PASS, f"min {lo:.6%}, max {hi:.6%}")

    n_off = _one(con, f"SELECT count(*) FROM {view} WHERE epoch_ms(funding_time) % {HOUR_MS} <> 0")[
        0
    ]
    rep.add("funding on the hour", WARN if n_off else PASS, f"{n_off} events not on :00:00")

    dist = con.execute(
        f"""WITH d AS (SELECT DISTINCT epoch_ms(funding_time) AS t FROM {view}),
                 g AS (SELECT t - lag(t) OVER (ORDER BY t) AS dt FROM d)
            SELECT dt / {HOUR_MS}.0 AS hours, count(*) FROM g WHERE dt IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall()
    modal = dist[0][0] if dist else None
    # An interval that differs from the usual one is fine when the exchange announced it on the
    # event itself (bulk rows carry funding_interval_hours, e.g. 8h -> 4h during volatility).
    base = f"""WITH d AS (SELECT epoch_ms(funding_time) AS t,
                                 any_value(funding_interval_hours) AS ih
                          FROM {view} GROUP BY 1),
                    g AS (SELECT lag(t) OVER (ORDER BY t) AS prev, t, ih FROM d)
               SELECT prev, t, (t - prev) / {HOUR_MS}.0 FROM g
               WHERE prev IS NOT NULL AND (t - prev) <> {int((modal or 0) * HOUR_MS)}"""
    irregular = con.execute(
        f"{base} AND (ih IS NULL OR (t - prev) <> ih * {HOUR_MS}) ORDER BY prev"
    ).fetchall()
    announced = con.execute(f"{base} AND (t - prev) = ih * {HOUR_MS}").fetchall()
    rep.add(
        "interval regularity",
        WARN if irregular else PASS,
        "intervals (h: count): "
        + ", ".join(f"{h:g}: {c}" for h, c in dist)
        + f"; {len(announced)} match an announced interval change, {len(irregular)} unexplained",
    )
    for prev, t, hours in irregular:
        gap_events = round(hours / modal) - 1 if modal and hours > modal else 0
        rep.gaps.append(
            Gap(
                from_ms(prev),
                from_ms(t),
                max(gap_events, 0),
                f"interval {hours:g}h vs usual {modal:g}h",
            )
        )
    if start_ms is not None and modal and first - start_ms > modal * HOUR_MS:
        rep.add("coverage from start", WARN, f"first event {from_ms(first)} after start")
    if modal:
        behind = (now_ms - last) // int(modal * HOUR_MS)
        rep.add(
            "up to date",
            PASS if behind <= 0 else WARN,
            "current" if behind <= 0 else f"~{behind} newer events not downloaded (run update)",
        )
    return rep


# ---------------------------------------------------------------- mark vs last


def check_mark_vs_last(
    con: duckdb.DuckDBPyConnection,
    last_view: str,
    mark_view: str,
    ds: Dataset,
    threshold: float = MARK_LAST_THRESHOLD,
) -> MarkLastReport:
    """Flag candles whose mark close deviates from the last-price close by more than threshold.

    Consecutive flagged candles are grouped into episodes (typically liquidation cascades or
    last-price wicks). Report only: large divergence is real market behaviour, not bad data.
    """
    rep = MarkLastReport(ds.timeframe or "", threshold)
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE _ml AS
            SELECT epoch_ms(l.open_time) AS t, l.close AS last_close, m.close AS mark_close,
                   abs(m.close - l.close) / l.close AS dev
            FROM {last_view} l JOIN {mark_view} m ON epoch_ms(m.open_time) = epoch_ms(l.open_time)
            WHERE l.close > 0"""
    )
    rep.compared, rep.flagged, med, p99, mx = _one(
        con,
        f"""SELECT count(*), count(*) FILTER (WHERE dev > {threshold}),
                   coalesce(median(dev), 0), coalesce(quantile_cont(dev, 0.99), 0),
                   coalesce(max(dev), 0) FROM _ml""",
    )
    rep.median_dev, rep.p99_dev, rep.max_dev = float(med), float(p99), float(mx)
    iv = ds.interval_ms
    rows = con.execute(
        f"""WITH f AS (SELECT t, dev, t - {iv} * row_number() OVER (ORDER BY t) AS grp
                       FROM _ml WHERE dev > {threshold})
            SELECT min(t), max(t), count(*), max(dev) FROM f GROUP BY grp ORDER BY 4 DESC"""
    ).fetchall()
    rep.episodes = [(from_ms(a), from_ms(b), n, float(d)) for a, b, n, d in rows]
    return rep


# ---------------------------------------------------------------- orchestration


def verify_store(
    store: Store,
    datasets: tuple[Dataset, ...] = DEFAULT_DATASETS,
    now: datetime | None = None,
) -> VerifyReport:
    now_ms = to_ms(now or datetime.now(UTC))
    manifest = store.load_manifest()
    start = date.fromisoformat(manifest["start"]) if manifest.get("start") else None
    start_ms = date_ms(start) if start else None
    con = duckdb_connect()
    views: dict[str, str] = {}
    reports: list[DatasetReport] = []
    for ds in datasets:
        view = "v_" + ds.key.replace("/", "_")
        if not create_view(con, view, store, ds):
            rep = DatasetReport(ds.key)
            rep.add("data present", FAIL, "no parquet files")
            reports.append(rep)
            continue
        views[ds.key] = view
        complete = {
            m for m, e in manifest.get("datasets", {}).get(ds.key, {}).items() if e.get("complete")
        }
        rec = manifest.get("reconcile", {}).get(ds.key, {})
        if ds.is_candles:
            reports.append(
                check_candles(
                    con,
                    view,
                    ds,
                    start_ms=start_ms,
                    now_ms=now_ms,
                    complete_months=complete,
                    rest_checked=set(rec.get("rest_checked", [])),
                    replaced=len(rec.get("replaced", [])),
                )
            )
        else:
            reports.append(check_funding(con, view, start_ms=start_ms, now_ms=now_ms))
    mark_last = [
        check_mark_vs_last(con, views[last.key], views[mark.key], last)
        for last, mark in MARK_LAST_CHECKS
        if last.key in views and mark.key in views
    ]
    confirmed = manifest.get("reconcile", {}).get("cross_tf_confirmed", {})
    cross = [
        check_cross_tf(
            con, views[s.key], views[d.key], s, d, set(confirmed.get(f"{s.key}->{d.key}", []))
        )
        for s, d in CROSS_TF_CHECKS
        if s.key in views and d.key in views
    ]
    con.close()
    return VerifyReport(store.symbol, from_ms(now_ms), start, reports, cross, mark_last)


# ---------------------------------------------------------------- cross-symbol


def annotate_cross_symbol(reports: list[VerifyReport]) -> None:
    """Tag each candle gap with the other symbols that have a gap overlapping the same time.

    A gap present on several symbols at once is an exchange-wide event (outage/maintenance),
    not a problem with one symbol's data.
    """
    spans = [
        (r.symbol, g) for r in reports for d in r.datasets if d.key != "funding" for g in d.gaps
    ]
    for r in reports:
        for d in r.datasets:
            if d.key == "funding":
                continue
            for g in d.gaps:
                others = sorted(
                    {
                        sym
                        for sym, o in spans
                        if sym != r.symbol
                        and o.first_missing <= g.last_missing
                        and g.first_missing <= o.last_missing
                    }
                )
                g.also_in = others


# ---------------------------------------------------------------- markdown


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def _gap_where(g: Gap) -> str:
    if not g.also_in:
        return "this symbol only"
    return "also " + ", ".join(g.also_in) + " → exchange-wide"


def _symbol_section(rep: VerifyReport) -> list[str]:
    out = [
        "",
        f"## {rep.symbol}: {rep.status}",
        "",
        f"Requested start: {rep.start or '-'}",
        "",
        "| Dataset | Rows | First (UTC) | Last (UTC) | Gaps | Missing | Status |",
        "|---|---:|---|---|---:|---:|---|",
    ]
    for d in rep.datasets:
        out.append(
            f"| {d.key} | {d.rows:,} | {_fmt(d.first)} | {_fmt(d.last)} | {len(d.gaps)} | "
            f"{d.missing_total:,} | {d.status} |"
        )
    out += [
        "",
        "### Cross-timeframe consistency (5m resampled vs downloaded)",
        "",
        "| Source → target | Compared | OHLC mismatches | Confirmed exchange-side | "
        "Volume mismatches | Skipped (5m incomplete) | Missing in target | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for c in rep.cross_tf:
        out.append(
            f"| {c.source} → {c.target} | {c.compared:,} | {c.mismatches:,} | "
            f"{c.confirmed_source:,} | {c.volume_mismatches:,} | {c.skipped_incomplete:,} | "
            f"{c.missing_in_target:,} | {c.status} |"
        )
    for c in rep.cross_tf:
        if c.examples:
            out += ["", f"Mismatches {c.source} → {c.target} (bucket, o, O, h, H, l, L, c, C):"]
            out += [f"- {row}" for row in c.examples]
    if rep.mark_last:
        out += [
            "",
            "### Mark vs last price (report only)",
            "",
            "| TF | Compared | Threshold | Flagged candles | Episodes | Median | p99 | Max |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for m in rep.mark_last:
            out.append(
                f"| {m.timeframe} | {m.compared:,} | {m.threshold:.1%} | {m.flagged:,} | "
                f"{len(m.episodes)} | {m.median_dev:.4%} | {m.p99_dev:.4%} | {m.max_dev:.3%} |"
            )
        for m in rep.mark_last:
            if m.episodes:
                out += [
                    "",
                    f"{m.timeframe} episodes, largest first "
                    f"(showing {min(len(m.episodes), 20)} of {len(m.episodes)}):",
                    "",
                    "| From (UTC) | To (UTC) | Candles | Max deviation |",
                    "|---|---|---:|---:|",
                ]
                out += [
                    f"| {_fmt(a)} | {_fmt(b)} | {n} | {dev:.2%} |"
                    for a, b, n, dev in m.episodes[:20]
                ]
    for d in rep.datasets:
        out += ["", f"### {d.key}", "", "| Check | Status | Detail |", "|---|---|---|"]
        out += [f"| {c.name} | {c.status} | {c.detail} |" for c in d.checks]
        if d.gaps:
            funding = d.key == "funding"
            out += [
                "",
                f"{'Irregular intervals' if funding else 'Gaps'} ({len(d.gaps)}):",
                "",
                "| From (UTC) | To (UTC) | Missing | Note | Other symbols |",
                "|---|---|---:|---|---|",
            ]
            out += [
                f"| {_fmt(g.first_missing)} | {_fmt(g.last_missing)} | {g.missing} | {g.note} | "
                f"{'-' if funding else _gap_where(g)} |"
                for g in d.gaps[:MAX_LISTED]
            ]
            if len(d.gaps) > MAX_LISTED:
                out.append(f"| … | … | … | {len(d.gaps) - MAX_LISTED} more | |")
        for name, rows in d.examples.items():
            out += ["", f"{name} (first {len(rows)}):"] + [f"- {r}" for r in rows]
    return out


def render_run_markdown(reports: list[VerifyReport]) -> str:
    generated = max((r.generated_at for r in reports), default=datetime.now(UTC))
    overall = max((r.status for r in reports), key=_RANK.__getitem__, default=PASS)
    out = [
        f"# Data quality report: {generated:%Y-%m-%d}",
        "",
        f"- Generated: {generated:%Y-%m-%d %H:%M:%S} UTC",
        f"- Symbols: {', '.join(r.symbol for r in reports)}",
        f"- Overall status: **{overall}**",
        "",
        "Gaps are never filled or interpolated. For finalised months every gap was re-requested "
        "from the REST API after the bulk files; gaps that remain do not exist at the source. "
        "A gap that appears on several symbols at the same time is an exchange-wide event.",
        "",
        "Reconcile: every zero-trade candle and every cross-timeframe mismatch is re-downloaded "
        "from the REST API; where REST differs, REST replaces the bulk row. Mismatches that remain "
        "are the exchange's own data and are listed as 'confirmed exchange-side' (WARN).",
        "",
        f"Mark vs last price is report only (INFO): candles where the closes differ by more than "
        f"{MARK_LAST_THRESHOLD:.0%} are grouped into episodes.",
        "",
        "## Summary",
        "",
        "| Symbol | Status | Candles missing (gaps) | Cross-tf mismatches | "
        "Confirmed exchange-side | Mark-vs-last flagged 5m (episodes) | "
        "Funding irregular | REST replaced rows |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in reports:
        candles = [d for d in r.datasets if d.key != "funding"]
        funding = next((d for d in r.datasets if d.key == "funding"), None)
        m5 = next((m for m in r.mark_last if m.timeframe == "5m"), None)
        replaced = sum(d.rest_replaced for d in candles)
        out.append(
            f"| {r.symbol} | {r.status} | "
            f"{sum(d.missing_total for d in candles):,} ({sum(len(d.gaps) for d in candles)}) | "
            f"{sum(c.mismatches for c in r.cross_tf)} | "
            f"{sum(c.confirmed_source for c in r.cross_tf)} | "
            f"{f'{m5.flagged:,} ({len(m5.episodes)})' if m5 else '-'} | "
            f"{len(funding.gaps) if funding else '-'} | {replaced} |"
        )
    all_gaps = [
        (r.symbol, d.key, g)
        for r in reports
        for d in r.datasets
        if d.key != "funding"
        for g in d.gaps
    ]
    if all_gaps:
        out += [
            "",
            "## All gaps",
            "",
            "| Symbol | Dataset | From (UTC) | To (UTC) | Missing | Other symbols |",
            "|---|---|---|---|---:|---|",
        ]
        out += [
            f"| {sym} | {key} | {_fmt(g.first_missing)} | {_fmt(g.last_missing)} | {g.missing} | "
            f"{_gap_where(g)} |"
            for sym, key, g in sorted(all_gaps, key=lambda x: (x[2].first_missing, x[0]))
        ]
    confirmed: dict[tuple[int, str], list[str]] = {}
    for r in reports:
        for c in r.cross_tf:
            for b in c.confirmed_buckets:
                confirmed.setdefault((b, f"{c.source} → {c.target}"), []).append(r.symbol)
    if confirmed:
        out += [
            "",
            "## Confirmed exchange-side mismatches across symbols",
            "",
            "Same bucket on several symbols = exchange-wide incident (the exchange's own "
            "higher-timeframe candle disagrees with its 5m candles).",
            "",
            "| Bucket (UTC) | Check | Symbols |",
            "|---|---|---|",
        ]
        out += [
            f"| {from_ms(b):%Y-%m-%d %H:%M} | {pair} | {len(syms)}: {', '.join(sorted(syms))} |"
            for (b, pair), syms in sorted(confirmed.items())
        ]
    for r in reports:
        out += _symbol_section(r)
    return "\n".join(out) + "\n"


def write_run_report(reports_dir: Path, reports: list[VerifyReport]) -> str:
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated = max((r.generated_at for r in reports), default=datetime.now(UTC))
    path = reports_dir / f"verify_{generated:%Y-%m-%d}.md"
    path.write_text(render_run_markdown(reports), encoding="utf-8")
    return str(path)
