import type { Metadata } from "next";

import ChartView from "@/components/chart/ChartView";

export const metadata: Metadata = { title: "Chart · Deskmind" };

const DEFAULT_SYMBOL = "BTCUSDT";
const DEFAULT_TF = "15m";

export default async function ChartPage(props: PageProps<"/chart">) {
  const params = await props.searchParams;
  const symbol = typeof params.symbol === "string" ? params.symbol.toUpperCase() : DEFAULT_SYMBOL;
  const tf = typeof params.tf === "string" ? params.tf : DEFAULT_TF;
  const runId = typeof params.runId === "string" ? params.runId : null;
  return <ChartView initialSymbol={symbol} initialTf={tf} initialRunId={runId} />;
}
