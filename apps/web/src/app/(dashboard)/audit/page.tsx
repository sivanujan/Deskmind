"use client";

import { useEffect, useState } from "react";
import { api, type AuditLogEntry } from "@/lib/api";

export default function AuditLogPage() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [actionFilter, setActionFilter] = useState("");
  const [isLoading, setIsLoading] = useState(true);

  const loadAuditLogs = async () => {
    setIsLoading(true);
    try {
      const data = await api.auditLogs({
        action: actionFilter || undefined,
        limit: 100,
      });
      setLogs(data);
    } catch {
      // Ignored
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    let mounted = true;
    void api.auditLogs({
      action: actionFilter || undefined,
      limit: 100,
    }).then((data) => {
      if (mounted) {
        setLogs(data);
        setIsLoading(false);
      }
    }).catch(() => {
      if (mounted) setIsLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, [actionFilter]);

  return (
    <div className="flex-1 space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-bold text-foreground">Immutable Audit Trail</h1>
          <p className="text-xs text-muted">
            Append-only audit ledger recording every operator authentication event, mode toggle, and control execution.
          </p>
        </div>

        <button
          onClick={loadAuditLogs}
          className="rounded border border-border bg-surface px-3 py-1.5 text-xs text-muted hover:text-foreground"
        >
          Refresh Audit Trail
        </button>
      </div>

      {/* Filter */}
      <div className="flex items-center gap-3 rounded-lg border border-border bg-surface p-4 text-xs">
        <label className="text-muted">Filter by Action:</label>
        <select
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          className="rounded border border-border bg-background px-3 py-1 text-foreground"
        >
          <option value="">All Actions</option>
          <option value="login_success">login_success</option>
          <option value="login_failed">login_failed</option>
          <option value="service_control">service_control</option>
          <option value="mode_change">mode_change</option>
          <option value="kill_switch">kill_switch</option>
          <option value="breaker_reset">breaker_reset</option>
          <option value="backtest_submit">backtest_submit</option>
        </select>
      </div>

      {/* Audit Log Table */}
      <div className="rounded-lg border border-border bg-surface p-5">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="pb-2 font-medium">Timestamp (UTC)</th>
                <th className="pb-2 font-medium">User</th>
                <th className="pb-2 font-medium">Action</th>
                <th className="pb-2 font-medium">Status</th>
                <th className="pb-2 font-medium">IP Address</th>
                <th className="pb-2 font-medium">Details (JSON)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {logs.length > 0 ? (
                logs.map((log) => (
                  <tr key={log.id ?? `${log.created_at}-${log.action}`} className="hover:bg-surface-2/40">
                    <td suppressHydrationWarning className="py-2.5 font-mono text-muted">{new Date(log.created_at).toISOString().replace("T", " ").slice(0, 19)}</td>
                    <td className="py-2.5 font-semibold text-foreground">{log.user}</td>
                    <td className="py-2.5">
                      <span className="font-mono text-accent">{log.action}</span>
                    </td>
                    <td className="py-2.5">
                      <span
                        className={`rounded px-1.5 py-0.5 font-bold uppercase text-[10px] ${
                          log.status === "success"
                            ? "bg-up/20 text-up"
                            : "bg-down/20 text-down"
                        }`}
                      >
                        {log.status}
                      </span>
                    </td>
                    <td className="py-2.5 font-mono text-muted">{log.ip_address}</td>
                    <td className="py-2.5 font-mono text-[11px] text-muted max-w-xs truncate" title={JSON.stringify(log.details)}>
                      {JSON.stringify(log.details)}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-muted">
                    {isLoading ? "Loading audit logs..." : "No audit entries recorded yet."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
