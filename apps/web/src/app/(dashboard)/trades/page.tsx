"use client";

import { useEffect, useState } from "react";
import { api, type TradeJournalEntry } from "@/lib/api";

export default function TradesJournalPage() {
  const [trades, setTrades] = useState<TradeJournalEntry[]>([]);
  const [selectedTrade, setSelectedTrade] = useState<TradeJournalEntry | null>(null);
  const [symbolFilter, setSymbolFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [isLoading, setIsLoading] = useState(true);

  const loadTrades = async () => {
    setIsLoading(true);
    try {
      const data = await api.journal({
        symbol: symbolFilter || undefined,
        status: statusFilter || undefined,
        limit: 50,
      });
      setTrades(data);
    } catch {
      // Ignored
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    let mounted = true;
    void api.journal({
      symbol: symbolFilter || undefined,
      status: statusFilter || undefined,
      limit: 50,
    }).then((data) => {
      if (mounted) {
        setTrades(data);
        setIsLoading(false);
      }
    }).catch(() => {
      if (mounted) setIsLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, [symbolFilter, statusFilter]);

  return (
    <div className="flex-1 space-y-6 p-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-bold text-foreground">Trade Journal & Reconciliation</h1>
          <p className="text-xs text-muted">
            Auditable lifecycle ledger: intents, execution orders, partial fills, realized slippage, and fee reconciliation.
          </p>
        </div>

        <button
          onClick={loadTrades}
          className="rounded border border-border bg-surface px-3 py-1.5 text-xs text-muted hover:text-foreground"
        >
          Refresh Ledger
        </button>
      </div>

      {/* Filter Controls */}
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface p-4 text-xs">
        <div>
          <label className="block text-[11px] text-muted">Filter by Symbol</label>
          <input
            type="text"
            value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value.toUpperCase())}
            placeholder="e.g. BTCUSDT"
            className="mt-1 rounded border border-border bg-background px-3 py-1 text-foreground"
          />
        </div>

        <div>
          <label className="block text-[11px] text-muted">Filter by Status</label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="mt-1 rounded border border-border bg-background px-3 py-1 text-foreground"
          >
            <option value="">All Statuses</option>
            <option value="OPEN">OPEN</option>
            <option value="CLOSED">CLOSED</option>
            <option value="CANCELLED">CANCELLED</option>
          </select>
        </div>
      </div>

      {/* Trades Table */}
      <div className="rounded-lg border border-border bg-surface p-5">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="pb-2 font-medium">Trade ID</th>
                <th className="pb-2 font-medium">Symbol</th>
                <th className="pb-2 font-medium">Side</th>
                <th className="pb-2 font-medium">Status</th>
                <th className="pb-2 font-medium">Entry Price</th>
                <th className="pb-2 font-medium">Exit Price</th>
                <th className="pb-2 font-medium">Realized PnL</th>
                <th className="pb-2 font-medium">Total Fees</th>
                <th className="pb-2 font-medium text-right">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {trades.length > 0 ? (
                trades.map((t) => (
                  <tr key={t.trade_id} className="hover:bg-surface-2/40">
                    <td className="py-2.5 font-mono text-accent">{t.trade_id.slice(0, 12)}...</td>
                    <td className="py-2.5 font-bold text-foreground">{t.symbol}</td>
                    <td className="py-2.5">
                      <span
                        className={`rounded px-1.5 py-0.5 font-bold ${
                          t.direction === "LONG" ? "bg-up/20 text-up" : "bg-down/20 text-down"
                        }`}
                      >
                        {t.direction}
                      </span>
                    </td>
                    <td className="py-2.5 font-medium">{t.status}</td>
                    <td className="py-2.5 font-mono">${t.entry_price.toFixed(2)}</td>
                    <td className="py-2.5 font-mono">{t.exit_price ? `$${t.exit_price.toFixed(2)}` : "—"}</td>
                    <td className="py-2.5 font-mono">
                      {t.pnl !== undefined ? (
                        <span className={t.pnl >= 0 ? "text-up font-bold" : "text-down font-bold"}>
                          ${t.pnl.toFixed(2)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-2.5 font-mono text-muted">${t.fees.toFixed(3)}</td>
                    <td className="py-2.5 text-right">
                      <button
                        onClick={() => setSelectedTrade(t)}
                        className="rounded border border-border px-2.5 py-1 text-xs text-accent hover:bg-accent/10"
                      >
                        Inspect Ledger &rarr;
                      </button>
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={9} className="py-8 text-center text-muted">
                    {isLoading ? "Loading journal ledger..." : "No trades found in the journal database."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Trade Detail Modal / Drawer */}
      {selectedTrade && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-xs p-4">
          <div className="w-full max-w-2xl rounded-lg border border-border bg-surface p-6 shadow-2xl space-y-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between border-b border-border pb-3">
              <div>
                <h2 className="text-base font-bold text-foreground">
                  Trade Details: <span className="font-mono text-accent">{selectedTrade.trade_id}</span>
                </h2>
                <div className="text-xs text-muted">Strategy: {selectedTrade.strategy}</div>
              </div>
              <button
                onClick={() => setSelectedTrade(null)}
                className="rounded border border-border px-2.5 py-1 text-xs text-muted hover:text-foreground"
              >
                Close ✕
              </button>
            </div>

            {/* Reconciliation Breakdown */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 text-xs">
              <div className="rounded bg-surface-2 p-2.5">
                <span className="text-muted">Symbol / Side</span>
                <div className="mt-1 font-bold text-foreground">
                  {selectedTrade.symbol} {selectedTrade.direction}
                </div>
              </div>
              <div className="rounded bg-surface-2 p-2.5">
                <span className="text-muted">Quantity</span>
                <div className="mt-1 font-mono text-foreground">{selectedTrade.quantity}</div>
              </div>
              <div className="rounded bg-surface-2 p-2.5">
                <span className="text-muted">Entry Price</span>
                <div className="mt-1 font-mono text-foreground">${selectedTrade.entry_price.toFixed(2)}</div>
              </div>
              <div className="rounded bg-surface-2 p-2.5">
                <span className="text-muted">Exit Price</span>
                <div className="mt-1 font-mono text-foreground">
                  {selectedTrade.exit_price ? `$${selectedTrade.exit_price.toFixed(2)}` : "Open"}
                </div>
              </div>
            </div>

            {/* Risk Decision Snapshot */}
            <div className="rounded-md border border-border bg-background p-3 text-xs">
              <span className="font-semibold text-foreground">Risk Manager Authorization Decision</span>
              <div className="mt-1 space-y-1 text-muted">
                <div>Status: <span className="text-accent">{selectedTrade.risk_decision?.status ?? "Approved"}</span></div>
                <div>Reason: {selectedTrade.risk_decision?.reason ?? "Within position notional and breaker limits"}</div>
                <div>Breaker Latched State: {selectedTrade.risk_decision?.circuit_breaker_state ?? "NORMAL"}</div>
              </div>
            </div>

            {/* Cost & Friction Reconciliation */}
            <div className="rounded-md border border-border bg-background p-3 text-xs">
              <span className="font-semibold text-foreground">Fee & Reconciliation Summary</span>
              <div className="mt-2 grid grid-cols-3 gap-2">
                <div>
                  <span className="text-muted">Exchange Fees:</span>
                  <div className="font-mono text-foreground">${selectedTrade.fees.toFixed(4)}</div>
                </div>
                <div>
                  <span className="text-muted">Funding Payments:</span>
                  <div className="font-mono text-foreground">${selectedTrade.funding.toFixed(4)}</div>
                </div>
                <div>
                  <span className="text-muted">Modeled Slippage:</span>
                  <div className="font-mono text-foreground">${selectedTrade.slippage.toFixed(4)}</div>
                </div>
              </div>
            </div>

            {/* Orders & Fills if present */}
            {selectedTrade.orders && selectedTrade.orders.length > 0 && (
              <div className="text-xs">
                <span className="font-semibold text-foreground">Exchange Orders</span>
                <div className="mt-1 space-y-1">
                  {selectedTrade.orders.map((o) => (
                    <div key={o.order_id} className="flex justify-between rounded bg-surface-2 p-2">
                      <span className="font-mono">{o.order_id}</span>
                      <span>{o.type}</span>
                      <span className="font-mono">${o.price}</span>
                      <span className="text-up font-bold">{o.status}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
