"use client";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type ISeriesPrimitive,
  type LogicalRange,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";

import type { Candle } from "@/lib/api";
import type { Overlay } from "@/lib/overlays";

import { LinePrimitive, ZonePrimitive } from "./overlayPrimitives";

const UP = "#26a69a";
const DOWN = "#ef5350";

export interface HoverInfo {
  candle: Candle;
  isLive: boolean;
}

export interface PriceChartHandle {
  /** Replace all data (symbol / timeframe switch). */
  reset(candles: Candle[]): void;
  /** Add older candles on the left, keeping the current view in place. */
  prepend(candles: Candle[]): void;
  /** Insert or update one candle (live partial update or a newly closed candle). */
  upsert(candle: Candle, isOpen: boolean): void;
  firstTime(): number | null;
}

interface Props {
  overlays: Overlay[];
  onReachLeftEdge: () => void;
  onHover: (info: HoverInfo | null) => void;
}

const toBar = (c: Candle): CandlestickData<Time> => ({
  time: c.time as UTCTimestamp,
  open: c.open,
  high: c.high,
  low: c.low,
  close: c.close,
});

const toVol = (c: Candle): HistogramData<Time> => ({
  time: c.time as UTCTimestamp,
  value: c.volume,
  color: c.close >= c.open ? `${UP}66` : `${DOWN}66`,
});

const PriceChart = forwardRef<PriceChartHandle, Props>(function PriceChart(
  { overlays, onReachLeftEdge, onHover },
  ref,
) {
  const container = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const primitivesRef = useRef<ISeriesPrimitive<Time>[]>([]);
  const data = useRef<Map<number, Candle>>(new Map());
  const liveTime = useRef<number | null>(null);
  const edgeCb = useRef(onReachLeftEdge);
  const hoverCb = useRef(onHover);

  useEffect(() => {
    edgeCb.current = onReachLeftEdge;
    hoverCb.current = onHover;
  }, [onReachLeftEdge, onHover]);

  // Create the chart once.
  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#0b0e14" },
        textColor: "#8a90a2",
        fontSize: 12,
        attributionLogo: true, // required TradingView attribution (Lightweight Charts licence)
      },
      grid: { vertLines: { color: "#1c2130" }, horzLines: { color: "#1c2130" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2f3d" },
      timeScale: { borderColor: "#2a2f3d", timeVisible: true, secondsVisible: false, rightOffset: 6 },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderVisible: false,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.25 } });

    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volume;
    markersRef.current = createSeriesMarkers(candles, []);

    const onRange = (range: LogicalRange | null) => {
      if (range && range.from < 20) edgeCb.current();
    };
    const onMove = (param: MouseEventParams<Time>) => {
      const t = typeof param.time === "number" ? param.time : null;
      const c = t !== null ? data.current.get(t) : undefined;
      hoverCb.current(c ? { candle: c, isLive: t === liveTime.current } : null);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    chart.subscribeCrosshairMove(onMove);
    return () => {
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useImperativeHandle(ref, () => {
    const setAll = () => {
      const sorted = [...data.current.values()].sort((a, b) => a.time - b.time);
      candleRef.current?.setData(sorted.map(toBar));
      volumeRef.current?.setData(sorted.map(toVol));
    };
    return {
      reset(candles) {
        data.current = new Map(candles.map((c) => [c.time, c]));
        liveTime.current = null;
        setAll();
        chartRef.current?.timeScale().scrollToRealTime();
      },
      prepend(candles) {
        const ts = chartRef.current?.timeScale();
        const before = ts?.getVisibleLogicalRange();
        let added = 0;
        for (const c of candles) {
          if (!data.current.has(c.time)) {
            data.current.set(c.time, c);
            added += 1;
          }
        }
        if (!added) return;
        setAll();
        if (ts && before) ts.setVisibleLogicalRange({ from: before.from + added, to: before.to + added });
      },
      upsert(candle, isOpen) {
        const last = candleRef.current?.data().at(-1);
        if (last && (last.time as number) > candle.time) return; // never rewrite older bars live
        data.current.set(candle.time, candle);
        liveTime.current = isOpen ? candle.time : null;
        candleRef.current?.update(toBar(candle));
        volumeRef.current?.update(toVol(candle));
      },
      firstTime() {
        const first = candleRef.current?.data()[0];
        return first ? (first.time as number) : null;
      },
    };
  }, []);

  // Overlays: markers through the markers plugin, lines/zones as primitives.
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    for (const p of primitivesRef.current) series.detachPrimitive(p);
    primitivesRef.current = [];
    const markers: SeriesMarker<Time>[] = [];
    for (const o of overlays) {
      if (o.type === "marker") {
        markers.push({
          time: o.time as UTCTimestamp,
          position: o.position === "above" ? "aboveBar" : "belowBar",
          shape: o.shape,
          color: o.color,
          text: o.text,
        });
      } else {
        const p = o.type === "line" ? new LinePrimitive(o) : new ZonePrimitive(o);
        series.attachPrimitive(p);
        primitivesRef.current.push(p);
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    markersRef.current?.setMarkers(markers);
  }, [overlays]);

  return (
    <div className="absolute inset-0">
      <div ref={container} className="h-full w-full" />
    </div>
  );
});

export default PriceChart;
