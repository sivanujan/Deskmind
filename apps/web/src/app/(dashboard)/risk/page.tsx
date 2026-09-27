"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, RefreshButton, fmtAge, fmtTime, pct, useApi, val } from "@/components/ui";

// Every value here comes from GET /api/dashboard/risk: the Risk Manager's RiskConfig and hard caps
// (src/deskmind/risk/config.py, including its own comments as descriptions), the equity source per
// trading mode, and the breaker state from the trade service / data/risk_state.json.

export default function RiskPage() {
  const { data, error, loading, reload } = useApi(api.risk, 10000);
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; msg: string } | null>(null);

  const reset = async () => {
    setBusy(true);
    setFeedback(null);
    try {
      const r = await api.resetBreaker(confirm);
      setFeedback({ ok: true, msg: `Breaker reset: ${r.circuit_breaker}` });
      setConfirm("");
      await reload();
    } catch (e) {
      setFeedback({ ok: false, msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  // Group config fields by the section comment they sit under in risk/config.py.
  const sections: Record<string, string[]> = {};
  if (data) {
    for (const k of Object.keys(data.config)) {
      const g = data.groups[k] || "Other";
      (sections[g] ??= []).push(k);
    }
  }
  const b = data?.breaker;
  const bf = b?.file;

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader
        title="Risk Manager"
        subtitle="Live values of RiskConfig (risk/config.py). Descriptions are the comments in that file. Changed only by a human in a reviewed commit."
      >
        <RefreshButton onClick={() => void reload()} />
      </PageHeader>
      <ErrorBox error={error} />
      <Loading loading={loading} />

      {data && (
        <>
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Card title="Equity used for sizing" right={<Badge tone="accent">mode: {data.trading_mode}</Badge>}>
              <KV
                k="Current equity"
                v={data.current_equity.value ? `${data.current_equity.value} USDT` : val(null)}
                hint={data.current_equity.source}
              />
              <KV k="Available balance" v={data.current_equity.available ? `${data.current_equity.available} USDT` : val(null)} />
              <KV k="Status file age" v={fmtAge(data.current_equity.status_age_s)} />
              {data.current_equity.reason && <Empty>{data.current_equity.reason}</Empty>}
              <div className="rounded bg-surface-2 p-3">
                <div className="mb-1 text-xs font-semibold text-foreground">Equity source per mode</div>
                {Object.entries(data.equity_by_mode).map(([mode, e]) => (
                  <KV
                    key={mode}
                    k={
                      <span className={mode === data.trading_mode ? "font-semibold text-accent" : ""}>
                        {mode} {mode === data.trading_mode && "(active)"}
                      </span>
                    }
                    hint={`used when: ${e.used_when}`}
                    v={
                      <span className="font-sans">
                        {e.source}
                        {e.starting_equity && <span className="block font-mono">start {e.starting_equity}</span>}
                      </span>
                    }
                  />
                ))}
              </div>
            </Card>

            <Card
              title="Circuit breaker"
              right={
                b?.state ? <Badge tone={b.state === "normal" ? "up" : "down"}>{b.state}</Badge> : <Badge tone="muted">unknown</Badge>
              }
            >
              {b?.reason && <Empty>{b.reason}</Empty>}
              {b?.source && <KV k="Source" v={b.source} />}
              {b?.kill_switch_halted !== null && b?.kill_switch_halted !== undefined && (
                <KV k="Kill switch halted" v={val(b.kill_switch_halted)} />
              )}
              {bf && (
                <div className="rounded bg-surface-2 p-3">
                  <KV k="Peak equity" v={bf.peak_equity} />
                  <KV k="Start-of-day equity" v={bf.start_of_day_equity} />
                  <KV k="Drawdown from peak" v={pct(bf.drawdown_pct_of_peak)} />
                  <KV k="Consecutive losses" v={bf.consecutive_losses} />
                  <KV k="Cooldown until" v={bf.cooldown_until_ms ? fmtTime(bf.cooldown_until_ms) : "none"} />
                  <div className="mt-2 text-xs font-semibold text-foreground">Transitions</div>
                  {bf.transitions.length === 0 && <Empty>No transitions recorded.</Empty>}
                  {bf.transitions
                    .slice()
                    .reverse()
                    .slice(0, 10)
                    .map((t) => (
                      <div key={t.timestamp_ms} className="text-[11px] text-muted">
                        {fmtTime(t.timestamp_ms)}: {t.from_state} → {t.to_state} ({t.reason})
                      </div>
                    ))}
                </div>
              )}
              <div className="space-y-2 border-t border-border pt-3 text-xs">
                <p className="text-muted">
                  Reset (same as <code>deskmind risk reset</code>): only a <code>full_stop</code> in risk_state.json can be reset;
                  the backend refuses anything else. Type RESET to confirm.
                </p>
                <div className="flex gap-2">
                  <input
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    placeholder="RESET"
                    className="flex-1 rounded border border-border bg-background px-2 py-1 font-mono"
                  />
                  <button
                    disabled={confirm !== "RESET" || busy}
                    onClick={() => void reset()}
                    className="rounded border border-down/40 bg-down/20 px-3 py-1 font-semibold text-down disabled:opacity-40"
                  >
                    Reset breaker
                  </button>
                </div>
                {feedback && <div className={feedback.ok ? "text-up" : "text-down"}>{feedback.msg}</div>}
              </div>
            </Card>
          </div>

          <Card title="Hard caps (code constants, config can never exceed these)">
            <div className="grid grid-cols-1 gap-x-6 md:grid-cols-2">
              {Object.entries(data.hard_caps).map(([k, v]) => (
                <KV key={k} k={k} v={String(v)} />
              ))}
            </div>
          </Card>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {Object.entries(sections).map(([section, keys]) => (
              <Card key={section} title={section}>
                {keys.map((k) => (
                  <KV key={k} k={<span className="font-mono">{k}</span>} hint={data.notes[k] || undefined} v={val(data.config[k])} />
                ))}
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
