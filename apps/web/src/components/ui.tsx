"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

/**
 * Load one backend endpoint. There is deliberately no fallback value: when the request fails the
 * page shows the error, never invented data (see tests/test_no_fake_ui.py).
 */
export function useApi<T>(fn: () => Promise<T>, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });

  const reload = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    if (!intervalMs) return;
    // Do not poll while the tab is in the background; refresh as soon as it is visible again.
    const t = setInterval(() => {
      if (!document.hidden) void reload();
    }, intervalMs);
    const onVisible = () => {
      if (!document.hidden) void reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload, intervalMs]);

  return { data, error, loading, reload };
}

export function PageHeader({ title, subtitle, children }: { title: string; subtitle?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
      <div className="min-w-0 max-w-3xl">
        <h1 className="font-display text-2xl font-bold tracking-tight text-foreground">{title}</h1>
        {subtitle && <p className="mt-1 text-sm leading-relaxed text-muted">{subtitle}</p>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

export function Card({ title, right, children, className = "" }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`space-y-3 rounded-lg border border-border bg-surface p-5 ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between gap-2 pb-1">
          <h2 className="font-display text-[15px] font-semibold text-foreground">{title}</h2>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

/** A labelled number. `tone` colours the value only. */
export function Stat({ label, value, hint, tone }: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: "up" | "down" | "accent" | "warn" }) {
  const cls = tone ? { up: "text-up", down: "text-down", accent: "text-accent", warn: "text-warn" }[tone] : "text-foreground";
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="label-caps">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

/** Explicit "nothing here" state. Use it instead of zeros or placeholder numbers. */
export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-md border border-dashed border-border px-4 py-5 text-center text-xs leading-relaxed text-muted">{children}</div>;
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div role="alert" className="rounded-md border border-down/40 bg-down/10 p-3 text-xs text-down">
      Backend request failed: {error}. Nothing is shown in place of the missing data.
    </div>
  );
}

export function Loading({ loading }: { loading: boolean }) {
  return loading ? <div className="text-xs text-muted">Loading…</div> : null;
}

export function KV({ k, v, hint }: { k: ReactNode; v: ReactNode; hint?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-border/50 py-1.5 text-xs last:border-b-0">
      <span className="text-muted">
        {k}
        {hint && <span className="block text-[10px] opacity-70">{hint}</span>}
      </span>
      <span className="text-right font-mono tabular-nums text-foreground">{v}</span>
    </div>
  );
}

export function RefreshButton({ onClick }: { onClick: () => void }) {
  return (
    <button onClick={onClick} className="btn">
      Refresh
    </button>
  );
}

/** Render a value, or an em dash labelled "no data" when the backend has none. */
export function val(v: unknown): ReactNode {
  if (v === null || v === undefined || v === "") return <span className="text-muted">— no data</span>;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function pct(v: string | number | null | undefined, digits = 2): ReactNode {
  if (v === null || v === undefined || v === "") return val(null);
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

export function num(v: unknown, digits = 2): ReactNode {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return val(null);
  return Number(v).toFixed(digits);
}

export function fmtTime(v: string | number | null | undefined): ReactNode {
  if (v === null || v === undefined || v === "") return val(null);
  const d = typeof v === "number" ? new Date(v) : new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return <span suppressHydrationWarning>{d.toISOString().replace("T", " ").slice(0, 19)} UTC</span>;
}

export function fmtAge(seconds: number | null | undefined): ReactNode {
  if (seconds === null || seconds === undefined) return val(null);
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h ago`;
  return `${(seconds / 86400).toFixed(1)} d ago`;
}

export function Badge({ tone, children }: { tone: "up" | "down" | "accent" | "muted" | "warn"; children: ReactNode }) {
  const cls = {
    up: "border-up/35 bg-up/12 text-up",
    down: "border-down/35 bg-down/12 text-down",
    accent: "border-accent/35 bg-accent/12 text-accent",
    warn: "border-warn/35 bg-warn/12 text-warn",
    muted: "border-border bg-surface-2 text-muted",
  }[tone];
  return (
    <span className={`inline-flex items-center whitespace-nowrap rounded border px-1.5 py-px font-display text-[11px] font-semibold uppercase tracking-wide ${cls}`}>
      {children}
    </span>
  );
}
