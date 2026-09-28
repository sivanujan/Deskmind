"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import HealthBadge from "@/components/HealthBadge";
import { api, type LiveMessage, type LiveStatus, type SymbolEntry, type TrendReport } from "@/lib/api";
import { DEV_SAMPLE_AGENT, devSampleOverlays } from "@/lib/devOverlays";
import type { Overlay } from "@/lib/overlays";
import { TIMEFRAME_SECONDS, formatAge, formatColombo, formatUtc } from "@/lib/time";

import OverlayDetails from "./OverlayDetails";
import PriceChart, { type HoverInfo, type PriceChartHandle } from "./PriceChart";
import TrendPanel from "./TrendPanel";

const FALLBACK_TFS = ["5m", "15m", "1h", "2h"];
const PAGE = 500;
const STATUS_POLL_MS = 5000;

type WsState = "connecting" | "live" | "reconnecting" | "paused";

/** Overlay kinds grouped into toggles. */
const GROUPS: Record<string, string> = {
  swing_high: "swings",
  swing_low: "swings",
  bos: "BOS/CHoCH",
  choch: "BOS/CHoCH",
  bos_level: "BOS/CHoCH",
  choch_level: "BOS/CHoCH",
  sweep: "sweeps",
  trendline_support: "trend lines",
  trendline_resistance: "trend lines",
  trade_entry: "trades",
  trade_exit: "trades",
  trade_line: "trades",
  trade_sl: "trades",
};
const groupOf = (o: Overlay) => GROUPS[o.kind] ?? "other";

function fmtPrice(v: number): string {
  const digits = v >= 1000 ? 1 : v >= 10 ? 2 : 4;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtVol(v: number): string {
  return v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(2)}K` : v.toFixed(2);
}

/** "2026-09-26T10:15" (UTC) <-> UNIX seconds */
function toInput(t: number): string {
  return new Date(t * 1000).toISOString().slice(0, 16);
}
function fromInput(v: string): number {
  return Math.floor(Date.parse(`${v}:00Z`) / 1000);
}

export default function ChartView({
  initialSymbol,
  initialTf,
  initialRunId = null,
}: {
  initialSymbol: string;
  initialTf: string;
  initialRunId?: string | null;
}) {
  const router = useRouter();
  const chart = useRef<PriceChartHandle>(null);
  const [symbols, setSymbols] = useState<string[]>([initialSymbol]);
  const [entries, setEntries] = useState<SymbolEntry[]>([]);
  const [timeframes, setTimeframes] = useState<string[]>(FALLBACK_TFS);
  const [symbol, setSymbol] = useState(initialSymbol);
  const [tf, setTf] = useState(initialTf);
  const [error, setError] = useState<string | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [latest, setLatest] = useState<HoverInfo | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [wsState, setWsState] = useState<WsState>("connecting");
  const [apiOverlays, setApiOverlays] = useState<Overlay[]>([]);
  const [devOverlays, setDevOverlays] = useState<Overlay[]>([]);
  const [hiddenAgents, setHiddenAgents] = useState<Set<string>>(new Set());
  const [hiddenGroups, setHiddenGroups] = useState<Set<string>>(new Set());
  const [report, setReport] = useState<TrendReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  /** Replay time T (UNIX seconds, a candle close) or null for live. */
  const [replayT, setReplayT] = useState<number | null>(null);
  const [replayInput, setReplayInput] = useState("");
  const olderState = useRef({ loading: false, exhausted: false });

  const [backtestRuns, setBacktestRuns] = useState<Array<{ run_id: string; strategy_name: string; split: string }>>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRunId);

  const iv = TIMEFRAME_SECONDS[tf] ?? 900;
  const selectedEntry = entries.find((e) => e.symbol === symbol);
  const key = `${symbol}|${tf}|${replayT ?? "live"}|${selectedRunId ?? "none"}`;

  useEffect(() => {
    api
      .symbols()
      .then((r) => {
        setSymbols(r.symbols);
        setEntries(r.entries);
        setTimeframes(r.timeframes);
      })
      .catch((e: Error) => setError(`API unreachable: ${e.message}`));

    api
      .backtestRuns()
      .then((runs) => setBacktestRuns(runs))
      .catch(() => {});
  }, []);

  useEffect(() => {
    router.replace(`/chart?symbol=${symbol}&tf=${tf}`, { scroll: false });
  }, [router, symbol, tf]);

  // History, overlays and the trend report — live, or as of the replay time T.
  useEffect(() => {
    let cancelled = false;
    const k = `${symbol}|${tf}|${replayT ?? "live"}`;
    olderState.current = { loading: false, exhausted: false };
    // Replay: only candles that had closed by T (open_time + iv <= T).
    const end = replayT !== null ? replayT - iv + 1 : undefined;
    api
      .candles(symbol, tf, { limit: PAGE, end })
      .then((r) => {
        if (cancelled) return;
        chart.current?.reset(r.candles);
        const last = r.candles.at(-1);
        setLatest(last ? { candle: last, isLive: false } : null);
        setDevOverlays(devSampleOverlays(r.candles));
        setError(null);
        return api.overlays(symbol, tf, {
          start: r.candles[0]?.time,
          asOf: replayT ?? undefined,
          runId: selectedRunId ?? undefined,
        });
      })
      .then((o) => {
        if (!cancelled && o) setApiOverlays(o.overlays);
      })
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoadedKey(k));
    api
      .trendReport(symbol, replayT ?? undefined)
      .then((r) => {
        if (cancelled) return;
        setReport(r);
        setReportError(null);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setReport(null);
        setReportError(e.message.startsWith("404") ? "no report for this time (run the backfill)" : e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, tf, replayT, iv, selectedRunId]);

  const loadOlder = useCallback(() => {
    const st = olderState.current;
    const end = chart.current?.firstTime();
    if (st.loading || st.exhausted || !end) return;
    st.loading = true;
    api
      .candles(symbol, tf, { end, limit: PAGE })
      .then((r) => {
        if (r.candles.length === 0) st.exhausted = true;
        else chart.current?.prepend(r.candles);
      })
      .catch(() => undefined)
      .finally(() => {
        st.loading = false;
      });
  }, [symbol, tf]);

  // Live updates (not in replay): partial candle in place, new candle on close.
  useEffect(() => {
    if (replayT !== null) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      setWsState(attempt === 0 ? "connecting" : "reconnecting");
      ws = new WebSocket(api.liveSocketUrl(symbol, tf));
      ws.onopen = () => {
        attempt = 0;
        setWsState("live");
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data as string) as LiveMessage;
        if (msg.symbol !== symbol || msg.timeframe !== tf) return;
        chart.current?.upsert(msg.candle, msg.is_open);
        setLatest({ candle: msg.candle, isLive: msg.is_open });
      };
      ws.onclose = () => {
        if (closed) return;
        setWsState("reconnecting");
        attempt += 1;
        timer = setTimeout(connect, Math.min(15000, 500 * 2 ** attempt));
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [symbol, tf, replayT]);

  useEffect(() => {
    let alive = true;
    const poll = () =>
      api
        .liveStatus()
        .then((s) => {
          if (!alive) return;
          setStatus(s);
          setStatusError(null);
        })
        .catch((e: Error) => alive && setStatusError(e.message));
    poll();
    const id = setInterval(poll, STATUS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const tfStatus = status?.symbols.find((s) => s.symbol === symbol)?.timeframes.find((t) => t.timeframe === tf);
  const symStatus = status?.symbols.find((s) => s.symbol === symbol);
  const effectiveWs: WsState = replayT !== null ? "paused" : wsState;
  const staleHere = !!statusError || !status?.running || !!tfStatus?.stale || effectiveWs !== "live";

  const allOverlays = useMemo(
    () => [...apiOverlays, ...(replayT === null ? devOverlays : [])],
    [apiOverlays, devOverlays, replayT],
  );
  const agents = useMemo(() => [...new Set(allOverlays.map((o) => o.source_agent))].sort(), [allOverlays]);
  const groups = useMemo(
    () => [...new Set(allOverlays.filter((o) => o.source_agent === "trend").map(groupOf))].sort(),
    [allOverlays],
  );
  const visibleOverlays = useMemo(
    () => allOverlays.filter((o) => !hiddenAgents.has(o.source_agent) && !hiddenGroups.has(groupOf(o))),
    [allOverlays, hiddenAgents, hiddenGroups],
  );
  const toggle = (setter: typeof setHiddenAgents, v: string) =>
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(v)) next.delete(v);
      else next.add(v);
      return next;
    });

  const loading = loadedKey !== key;
  const selectSymbol = (s: string) => {
    setHover(null);
    setSymbol(s);
  };
  const selectTf = (t: string) => {
    setHover(null);
    setTf(t);
    if (replayT !== null) {
      const ivNew = TIMEFRAME_SECONDS[t] ?? 900;
      setReplayT(Math.floor(replayT / ivNew) * ivNew);
    }
  };
  const startReplay = () => {
    const lastClose = latest ? latest.candle.time + (latest.isLive ? 0 : iv) : Math.floor(Date.now() / 1000);
    const t = Math.floor(lastClose / iv) * iv - 20 * iv;
    setReplayT(t);
    setReplayInput(toInput(t));
  };
  const stepReplay = (bars: number) => {
    if (replayT === null) return;
    const t = replayT + bars * iv;
    setReplayT(t);
    setReplayInput(toInput(t));
  };
  const applyReplayInput = (v: string) => {
    setReplayInput(v);
    const t = fromInput(v);
    if (Number.isFinite(t)) setReplayT(Math.floor(t / iv) * iv);
  };

  const shown = hover ?? latest;
  const c = shown?.candle;
  const change = c ? ((c.close - c.open) / c.open) * 100 : 0;
  const hoverOverlays = useMemo(() => {
    if (!hover) return [];
    const t = hover.candle.time;
    return visibleOverlays.filter((o) => o.type === "marker" && o.time === t);
  }, [hover, visibleOverlays]);
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const o of apiOverlays) if (o.type === "marker" || o.kind.startsWith("trendline")) m.set(groupOf(o), (m.get(groupOf(o)) ?? 0) + 1);
    return m;
  }, [apiOverlays]);

  return (
    <div className="flex flex-1 flex-col">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3 border-b border-border bg-surface px-4 py-2">
        <select
          aria-label="Symbol"
          value={symbol}
          onChange={(e) => selectSymbol(e.target.value)}
          className="rounded border border-border bg-surface-2 px-2 py-1 text-sm font-semibold"
        >
          {symbols.map((s) => {
            const e = entries.find((x) => x.symbol === s);
            const tags = [e?.state, e && !e.has_data ? "no data" : null].filter(Boolean);
            return (
              <option key={s} value={s}>
                {s}
                {tags.length ? ` · ${tags.join(" · ")}` : ""}
              </option>
            );
          })}
        </select>
        {selectedEntry?.state && (
          <span
            title="Universe lifecycle state"
            className={`rounded px-2 py-0.5 text-[11px] font-semibold uppercase ${
              selectedEntry.state === "tradeable"
                ? "bg-up/20 text-up"
                : selectedEntry.state === "blocked"
                  ? "bg-down/20 text-down"
                  : selectedEntry.state === "watch_only"
                    ? "bg-accent/20 text-accent"
                    : "bg-muted/20 text-muted"
            }`}
          >
            {selectedEntry.state.replace("_", " ")}
          </span>
        )}
        <div className="flex overflow-hidden rounded border border-border" role="group" aria-label="Timeframe">
          {timeframes.map((t) => (
            <button
              key={t}
              onClick={() => selectTf(t)}
              className={`px-3 py-1 text-sm ${t === tf ? "bg-accent/20 text-accent" : "text-muted hover:bg-surface-2"}`}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted">
          <span>Backtest:</span>
          <select
            value={selectedRunId ?? ""}
            onChange={(e) => setSelectedRunId(e.target.value || null)}
            className="rounded border border-border bg-surface-2 px-2 py-1 text-xs text-foreground"
          >
            <option value="">None</option>
            {backtestRuns.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id}
              </option>
            ))}
          </select>
        </div>
        <HealthBadge status={status} error={statusError} />
        <span className="text-xs text-muted" title="Chart WebSocket">
          ws: <span className={effectiveWs === "live" ? "text-up" : "text-warn"}>{effectiveWs}</span>
        </span>
        {symStatus?.mark_price && replayT === null && (
          <span className="text-xs text-muted">
            mark {fmtPrice(Number(symStatus.mark_price))} · funding{" "}
            {symStatus.funding_rate ? `${(Number(symStatus.funding_rate) * 100).toFixed(4)}%` : "—"} · mark age{" "}
            {formatAge(symStatus.mark_age_s)}
          </span>
        )}

        {/* Replay */}
        <div className="flex items-center gap-1.5 text-xs" aria-label="Replay">
          {replayT === null ? (
            <button onClick={startReplay} className="rounded border border-border px-2 py-1 hover:bg-surface-2">
              ⏮ Replay
            </button>
          ) : (
            <>
              <span className="rounded bg-warn/20 px-1.5 py-0.5 font-semibold text-warn">REPLAY</span>
              <button onClick={() => stepReplay(-1)} className="rounded border border-border px-2 py-1" title="Back one bar">
                ◀
              </button>
              <input
                type="datetime-local"
                aria-label="Replay time (UTC)"
                value={replayInput}
                onChange={(e) => applyReplayInput(e.target.value)}
                className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono"
              />
              <span className="text-muted">UTC</span>
              <button onClick={() => stepReplay(1)} className="rounded border border-border px-2 py-1" title="Forward one bar">
                ▶
              </button>
              <button onClick={() => setReplayT(null)} className="rounded border border-border px-2 py-1 hover:bg-surface-2">
                Exit
              </button>
            </>
          )}
        </div>

        {/* Overlay toggles */}
        <div className="ml-auto flex flex-wrap items-center gap-3 text-xs">
          <span className="text-muted">Overlays:</span>
          {agents.length === 0 && <span className="text-muted">none</span>}
          {agents.map((a) => (
            <label key={a} className="flex cursor-pointer items-center gap-1">
              <input type="checkbox" checked={!hiddenAgents.has(a)} onChange={() => toggle(setHiddenAgents, a)} />
              {a}
              {a === DEV_SAMPLE_AGENT && <span className="rounded bg-warn/20 px-1 text-warn">dev</span>}
            </label>
          ))}
          {groups.length > 0 && <span className="text-border">|</span>}
          {groups.map((g) => (
            <label key={g} className="flex cursor-pointer items-center gap-1 text-muted">
              <input type="checkbox" checked={!hiddenGroups.has(g)} onChange={() => toggle(setHiddenGroups, g)} />
              {g}
              <span className="text-[10px]">({counts.get(g) ?? 0})</span>
            </label>
          ))}
        </div>
      </div>

      {replayT !== null && (
        <div className="border-b border-warn/40 bg-warn/10 px-4 py-1 text-xs text-warn">
          Replay as of {formatUtc(replayT)} ({formatColombo(replayT)}): candles closed by then and only overlays
          with confirmed_at ≤ T — exactly what the agent knew at that moment. Live updates paused.
        </div>
      )}
      {replayT === null && status && staleHere && (
        <div className="border-b border-warn/40 bg-warn/10 px-4 py-1.5 text-xs text-warn">
          {statusError
            ? `API unreachable (${statusError}) — chart is not updating.`
            : !status.running
              ? "Live service is not running — the chart shows stored history only, no live updates."
              : effectiveWs !== "live"
                ? "Chart stream disconnected — reconnecting…"
                : `${symbol} ${tf} data is stale (last update ${formatAge(tfStatus?.stream_age_s)} ago).`}
        </div>
      )}
      {selectedEntry && !selectedEntry.has_data && (
        <div className="border-b border-warn/40 bg-warn/10 px-4 py-1.5 text-xs text-warn">
          {symbol}: no data downloaded yet
          {selectedEntry.state ? ` (universe state: ${selectedEntry.state.replace("_", " ")})` : ""}. The daily universe job
          downloads and verifies its history.
        </div>
      )}
      {error && <div className="border-b border-down/40 bg-down/10 px-4 py-1.5 text-xs text-down">{error}</div>}

      <TrendPanel report={report} timeframes={timeframes} error={reportError} />

      {/* Legend */}
      <div className="flex min-h-7 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-1 font-mono text-xs">
        {c ? (
          <>
            <span className="text-foreground">{symbol} · {tf}</span>
            <span>O <b className="font-normal text-foreground">{fmtPrice(c.open)}</b></span>
            <span>H <b className="font-normal text-foreground">{fmtPrice(c.high)}</b></span>
            <span>L <b className="font-normal text-foreground">{fmtPrice(c.low)}</b></span>
            <span>C <b className={`font-normal ${change >= 0 ? "text-up" : "text-down"}`}>{fmtPrice(c.close)}</b></span>
            <span className={change >= 0 ? "text-up" : "text-down"}>{change >= 0 ? "+" : ""}{change.toFixed(2)}%</span>
            <span>V <b className="font-normal text-foreground">{fmtVol(c.volume)}</b></span>
            <span className="text-muted">{formatUtc(c.time)}</span>
            <span className="text-muted">{formatColombo(c.time)}</span>
            {shown?.isLive && <span className="rounded bg-accent/20 px-1 text-accent">forming</span>}
          </>
        ) : (
          <span className="text-muted">{loading ? "Loading…" : "No data"}</span>
        )}
      </div>

      {/* Chart */}
      <div className="relative min-h-[420px] flex-1">
        <OverlayDetails overlays={hoverOverlays} />
        <PriceChart ref={chart} overlays={visibleOverlays} onReachLeftEdge={loadOlder} onHover={setHover} />
      </div>

      <footer className="border-t border-border px-4 py-1 text-[11px] text-muted">
        Charts by{" "}
        <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer" className="underline">
          TradingView
        </a>{" "}
        Lightweight Charts™ (Apache 2.0). Times shown in UTC and Asia/Colombo.
      </footer>
    </div>
  );
}
