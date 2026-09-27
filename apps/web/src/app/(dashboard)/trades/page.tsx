"use client";

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, RefreshButton, fmtTime, useApi, val } from "@/components/ui";

// Two separate ledgers, never mixed:
//  - Deskmind's own trades: trade_journal (GET /api/journal/trades)
//  - The user's own Binance history: external_trades (GET /api/external-trades, read-only import)

function Journal() {
  const [symbol, setSymbol] = useState("");
  const [status, setStatus] = useState("");
  const { data, error, loading, reload } = useApi(() => api.journal({ symbol: symbol || undefined, status: status || undefined, limit: 200 }));

  return (
    <Card
      title="Deskmind trade journal (paper / testnet)"
      right={
        <div className="flex gap-2 text-xs">
          <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="symbol" className="w-28 rounded border border-border bg-background px-2 py-1" />
          <input value={status} onChange={(e) => setStatus(e.target.value)} placeholder="status" className="w-24 rounded border border-border bg-background px-2 py-1" />
          <RefreshButton onClick={() => void reload()} />
        </div>
      }
    >
      <ErrorBox error={error} />
      <Loading loading={loading} />
      {data && data.length === 0 && <Empty>No trades in trade_journal yet.</Empty>}
      {data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-muted">
              <tr>
                {["Created", "Mode", "Symbol", "Side", "Entry", "SL / TP", "Size", "Lev", "Status", "Exit", "PnL", "client_order_id"].map((h) => (
                  <th key={h} className="pb-2 font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border font-mono">
              {data.map((t) => (
                <tr key={t.id}>
                  <td className="py-1.5">{fmtTime(t.created_at)}</td>
                  <td>{val(t.trading_mode)}</td>
                  <td className="font-bold">{t.symbol}</td>
                  <td className={t.side.toLowerCase().includes("long") || t.side === "BUY" ? "text-up" : "text-down"}>{t.side}</td>
                  <td>{t.entry}</td>
                  <td>
                    {t.sl} / {t.tp}
                  </td>
                  <td>{t.size}</td>
                  <td>{val(t.leverage)}</td>
                  <td>{t.status}</td>
                  <td>{t.exit_price !== null ? `${t.exit_price} (${t.exit_reason ?? "?"})` : val(null)}</td>
                  <td className={t.pnl === null ? "" : t.pnl >= 0 ? "text-up" : "text-down"}>{val(t.pnl)}</td>
                  <td className="text-muted">{t.client_order_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function toMs(date: string): number | undefined {
  if (!date) return undefined;
  const t = Date.parse(`${date}T00:00:00Z`);
  return Number.isNaN(t) ? undefined : t;
}

function BinanceHistory() {
  const [market, setMarket] = useState("");
  const [symbol, setSymbol] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [upload, setUpload] = useState<{ ok: boolean; msg: string } | null>(null);
  const [uploadMarket, setUploadMarket] = useState<"" | "spot" | "futures">("");

  const params = {
    market: market || undefined,
    symbol: symbol || undefined,
    start: toMs(from),
    end: to ? (toMs(to) ?? 0) + 86_400_000 : undefined,
  };
  const list = useApi(() => api.externalTrades({ ...params, limit: 500 }));
  const stats = useApi(() => api.externalTradeStats(params));
  const reload = () => {
    void list.reload();
    void stats.reload();
  };

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    setUpload(null);
    try {
      const r = await api.uploadTradeCsv(file, uploadMarket || undefined);
      setUpload({ ok: true, msg: `${file.name}: ${r.market}, ${r.rows} rows, ${r.inserted} new, ${r.duplicates} already present.` });
      reload();
    } catch (e) {
      setUpload({ ok: false, msg: e instanceof Error ? e.message : String(e) });
    }
  };

  const st = stats.data;
  const totals = list.data?.totals_by_market ?? {};

  return (
    <Card
      title="My Binance History (imported, read-only)"
      right={
        <span className="text-[11px] text-muted">
          Stored in external_trades, separate from the journal. Shown on the chart as source <code>binance:history</code>.
        </span>
      }
    >
      <div className="flex flex-wrap items-end gap-2 text-xs">
        <label>
          <span className="block text-muted">Market</span>
          <select value={market} onChange={(e) => setMarket(e.target.value)} className="rounded border border-border bg-background px-2 py-1">
            <option value="">all</option>
            <option value="spot">spot</option>
            <option value="futures">futures</option>
          </select>
        </label>
        <label>
          <span className="block text-muted">Symbol</span>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} className="w-28 rounded border border-border bg-background px-2 py-1" />
        </label>
        <label>
          <span className="block text-muted">From (UTC)</span>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="rounded border border-border bg-background px-2 py-1" />
        </label>
        <label>
          <span className="block text-muted">To (UTC)</span>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="rounded border border-border bg-background px-2 py-1" />
        </label>
        <button onClick={reload} className="rounded border border-border px-3 py-1">
          Apply
        </button>
        <div className="ml-auto flex items-end gap-2">
          <select value={uploadMarket} onChange={(e) => setUploadMarket(e.target.value as "" | "spot" | "futures")} className="rounded border border-border bg-background px-2 py-1">
            <option value="">detect market</option>
            <option value="spot">spot CSV</option>
            <option value="futures">futures CSV</option>
          </select>
          <label className="cursor-pointer rounded border border-accent/40 bg-accent/20 px-3 py-1 font-semibold text-accent">
            Upload Binance CSV
            <input
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(e) => {
                void onFile(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
          </label>
        </div>
      </div>
      {upload && <div className={`text-xs ${upload.ok ? "text-up" : "text-down"}`}>{upload.msg}</div>}
      <ErrorBox error={list.error || stats.error} />
      <Loading loading={list.loading} />

      <div className="text-[11px] text-muted">
        Stored in total:{" "}
        {Object.keys(totals).length === 0 ? "nothing imported yet" : Object.entries(totals).map(([k, n]) => `${k} ${n}`).join(", ")}. Import with{" "}
        <code>uv run deskmind account import-history</code> (read-only key) or upload a CSV export.
      </div>

      {st && st.trades === 0 && <Empty>No data yet: no imported fills match these filters.</Empty>}
      {st && st.trades > 0 && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <div className="rounded bg-surface-2 p-3">
            <div className="mb-1 text-xs font-semibold">Fills per symbol ({st.trades})</div>
            {Object.entries(st.per_symbol).map(([k, n]) => (
              <KV key={k} k={k} v={n} />
            ))}
          </div>
          <div className="rounded bg-surface-2 p-3">
            <div className="mb-1 text-xs font-semibold">Win / loss (fills with realized PnL)</div>
            {st.pnl_fills.winning + st.pnl_fills.losing === 0 ? (
              <Empty>No fills with realized PnL (spot fills have none).</Empty>
            ) : (
              <>
                <KV k="Winning" v={st.pnl_fills.winning} />
                <KV k="Losing" v={st.pnl_fills.losing} />
                <KV k="Win rate" v={st.pnl_fills.win_rate === null ? val(null) : `${(st.pnl_fills.win_rate * 100).toFixed(1)}%`} />
                <KV k="Realized PnL (sum)" v={st.pnl_fills.realized_pnl_total} />
              </>
            )}
            <p className="mt-1 text-[10px] text-muted">{st.pnl_fills.note}</p>
          </div>
          <div className="rounded bg-surface-2 p-3">
            <div className="mb-1 text-xs font-semibold">Fees paid</div>
            {Object.keys(st.fees_by_asset).length === 0 && <Empty>No fee data.</Empty>}
            {Object.entries(st.fees_by_asset).map(([a, f]) => (
              <KV key={a} k={a} v={f} />
            ))}
          </div>
        </div>
      )}

      {list.data && list.data.trades.length > 0 && (
        <div className="max-h-[480px] overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-surface text-muted">
              <tr>
                {["Time", "Market", "Symbol", "Side", "Price", "Qty", "Quote qty", "Fee", "Realized PnL", "Source", "Trade id", ""].map((h) => (
                  <th key={h} className="pb-2 font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border font-mono">
              {list.data.trades.map((t) => (
                <tr key={`${t.source}-${t.market}-${t.symbol}-${t.trade_id}`}>
                  <td className="py-1">{fmtTime(t.time)}</td>
                  <td>
                    <Badge tone={t.market === "futures" ? "accent" : "muted"}>{t.market}</Badge>
                  </td>
                  <td className="font-bold">{t.symbol}</td>
                  <td className={t.side === "BUY" ? "text-up" : "text-down"}>{t.side}</td>
                  <td>{t.price}</td>
                  <td>{t.qty}</td>
                  <td>{val(t.quote_qty)}</td>
                  <td>{t.fee !== null ? `${t.fee} ${t.fee_asset ?? ""}` : val(null)}</td>
                  <td>{val(t.realized_pnl)}</td>
                  <td>{t.source}</td>
                  <td className="text-muted">{t.trade_id}</td>
                  <td>
                    <Link href={`/chart?symbol=${encodeURIComponent(t.symbol)}`} className="text-accent underline">
                      chart
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function TradesPage() {
  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader title="Trades & Journal" subtitle="Deskmind's own trades and, separately, your imported Binance history." />
      <Journal />
      <BinanceHistory />
    </div>
  );
}
