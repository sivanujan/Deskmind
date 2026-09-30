/**
 * Canvas primitives for overlay lines and zones (Lightweight Charts v5 series primitives).
 * Markers use the built-in series-markers plugin instead (see PriceChart).
 */
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  SeriesType,
  Time,
  UTCTimestamp,
} from "lightweight-charts";

import type { LineOverlay, ZoneOverlay } from "@/lib/overlays";

type Point = { x: number; y: number };
type CanvasRenderingTarget2D = Parameters<IPrimitivePaneRenderer["draw"]>[0];

abstract class OverlayPrimitive implements ISeriesPrimitive<Time> {
  protected chart: IChartApi | null = null;
  protected series: ISeriesApi<SeriesType> | null = null;
  private readonly views: readonly IPrimitivePaneView[];

  constructor() {
    const renderer: IPrimitivePaneRenderer = { draw: (t) => this.draw(t) };
    this.views = [{ renderer: () => renderer, zOrder: () => this.zOrder() }];
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart as IChartApi;
    this.series = param.series as ISeriesApi<SeriesType>;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  protected point(time: number, price: number): Point | null {
    if (!this.chart || !this.series) return null;
    const x = this.chart.timeScale().timeToCoordinate(time as UTCTimestamp);
    const y = this.series.priceToCoordinate(price);
    return x === null || y === null ? null : { x, y };
  }

  protected zOrder(): "bottom" | "normal" | "top" {
    return "normal";
  }

  protected abstract draw(target: CanvasRenderingTarget2D): void;
}

export class LinePrimitive extends OverlayPrimitive {
  constructor(private readonly o: LineOverlay) {
    super();
  }

  protected draw(target: CanvasRenderingTarget2D): void {
    const a = this.point(this.o.start_time, this.o.start_price);
    const b = this.point(this.o.end_time, this.o.end_price);
    if (!a || !b) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      ctx.save();
      const faded = !!(this.o as { isHistorical?: boolean }).isHistorical;
      ctx.globalAlpha = faded ? 0.25 : 1.0;
      ctx.strokeStyle = this.o.color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(this.o.style === "dashed" ? [6, 4] : this.o.style === "dotted" ? [2, 3] : []);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      if (this.o.label) {
        ctx.setLineDash([]);
        ctx.fillStyle = this.o.color;
        ctx.font = "11px sans-serif";
        ctx.fillText(this.o.label, b.x + 4, b.y - 4);
      }
      ctx.restore();
    });
  }
}

export class ZonePrimitive extends OverlayPrimitive {
  constructor(private readonly o: ZoneOverlay) {
    super();
  }

  protected zOrder(): "bottom" {
    return "bottom";
  }

  protected draw(target: CanvasRenderingTarget2D): void {
    const a = this.point(this.o.start_time, this.o.top);
    const b = this.point(this.o.end_time, this.o.bottom);
    if (!a || !b) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      const x = Math.min(a.x, b.x);
      const y = Math.min(a.y, b.y);
      const w = Math.abs(b.x - a.x);
      const h = Math.abs(b.y - a.y);
      const faded = !!(this.o as { isHistorical?: boolean }).isHistorical;
      ctx.save();
      ctx.globalAlpha = faded ? 0.15 : 1.0;
      ctx.fillStyle = this.o.color;
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = this.o.color.length === 9 ? this.o.color.slice(0, 7) : this.o.color;
      ctx.globalAlpha = faded ? 0.2 : 0.6;
      ctx.strokeRect(x, y, w, h);
      if (this.o.label) {
        ctx.globalAlpha = faded ? 0.3 : 0.9;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.font = "11px sans-serif";
        ctx.fillText(this.o.label, x + 4, y + 13);
      }
      ctx.restore();
    });
  }
}
