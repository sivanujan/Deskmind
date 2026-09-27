"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type StrategyInfo } from "@/lib/api";

export default function StrategiesPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);

  useEffect(() => {
    api.strategies()
      .then(setStrategies)
      .catch(() => {
        setStrategies([
          {
            id: "trend_follow_v1",
            name: "trend_follow_v1",
            description: "Break of Structure (BOS) breakout entry combined with dynamic ascending/descending trendline bounces and ATR trailing stop.",
            timeframe: "15m",
            edge_status: "no_edge",
            parameters: {
              swing_lookback: 5,
              atr_period: 14,
              risk_fraction: 0.01,
              risk_reward_ratio: 2.0,
            },
            author: "Deskmind Quantitative Research",
          },
        ]);
      });
  }, []);

  return (
    <div className="flex-1 space-y-6 p-6">
      <div className="border-b border-border pb-4">
        <h1 className="text-xl font-bold text-foreground">Strategy Catalog & Edge Registry</h1>
        <p className="text-xs text-muted">
          Active quantitative trading strategies, edge validation status, parameters, and simulation shortcuts.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
        {strategies.map((strat) => (
          <div key={strat.id} className="rounded-lg border border-border bg-surface p-5 flex flex-col justify-between space-y-4">
            <div>
              <div className="flex items-center justify-between border-b border-border pb-2">
                <h2 className="font-mono text-sm font-bold text-accent">{strat.name}</h2>
                <span
                  className={`rounded px-2 py-0.5 text-xs font-semibold uppercase ${
                    strat.edge_status === "verified"
                      ? "bg-up/20 text-up"
                      : strat.edge_status === "no_edge"
                      ? "bg-down/20 text-down"
                      : "bg-muted/20 text-muted"
                  }`}
                >
                  {strat.edge_status.replace("_", " ")}
                </span>
              </div>

              <p className="mt-3 text-xs text-muted leading-relaxed">{strat.description}</p>

              <div className="mt-4 rounded bg-surface-2 p-3 text-xs space-y-1">
                <span className="font-semibold text-foreground">Fixed Parameters:</span>
                {Object.entries(strat.parameters).map(([k, v]) => (
                  <div key={k} className="flex justify-between font-mono text-[11px] text-muted">
                    <span>{k}:</span>
                    <span className="text-foreground">{String(v)}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="border-t border-border pt-3">
              <Link
                href="/backtest"
                className="block w-full text-center rounded bg-accent/20 border border-accent/40 py-2 text-xs font-semibold text-accent hover:bg-accent/30 transition-colors"
              >
                Launch Backtest &rarr;
              </Link>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
