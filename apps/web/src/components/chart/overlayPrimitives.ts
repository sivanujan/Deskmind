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

  protected getX(time: number): number | null {
    if (!this.chart || !this.series) return null;
    const timeScale = this.chart.timeScale();
    const direct = timeScale.timeToCoordinate(time as UTCTimestamp);
    if (direct !== null) return direct;

    const data = (this.series as unknown as { data?: () => readonly { time: Time }[] }).data?.();
    if (!data || data.length === 0) return null;

    const firstTime = Number(data[0].time);
    const lastTime = Number(data[data.length - 1].time);

    // If time is on or after the last candle (e.g. active zones extending into the future or view_end):
    if (time >= lastTime) {
      const lastIdx = data.length - 1;
      const xLast = timeScale.timeToCoordinate(data[lastIdx].time as UTCTimestamp) ??
                    timeScale.logicalToCoordinate(lastIdx as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
      const xPrev = timeScale.logicalToCoordinate(Math.max(0, lastIdx - 1) as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
      if (xLast !== null && xPrev !== null && lastIdx > 0) {
        const barWidth = Math.max(1, xLast - xPrev);
        const dt = Math.max(1, lastTime - Number(data[lastIdx - 1].time));
        return xLast + ((time - lastTime) / dt) * barWidth;
      }
      return xLast;
    }

    // If time is on or before the first candle (e.g. zones formed before loaded history):
    if (time <= firstTime) {
      const xFirst = timeScale.timeToCoordinate(data[0].time as UTCTimestamp) ??
                     timeScale.logicalToCoordinate(0 as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
      const xNext = timeScale.logicalToCoordinate(Math.min(data.length - 1, 1) as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
      if (xFirst !== null && xNext !== null && data.length > 1) {
        const barWidth = Math.max(1, xNext - xFirst);
        const dt = Math.max(1, Number(data[1].time) - firstTime);
        return xFirst + ((time - firstTime) / dt) * barWidth;
      }
      return xFirst;
    }

    // Between first and last candle: binary search for surrounding bars
    let low = 0;
    let high = data.length - 1;
    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      const midTime = Number(data[mid].time);
      if (midTime === time) {
        return timeScale.timeToCoordinate(data[mid].time as UTCTimestamp) ??
               timeScale.logicalToCoordinate(mid as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
      }
      if (midTime < time) {
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    const idxA = Math.max(0, high);
    const idxB = Math.min(data.length - 1, low);
    const tA = Number(data[idxA].time);
    const tB = Number(data[idxB].time);
    const xA = timeScale.timeToCoordinate(data[idxA].time as UTCTimestamp) ??
               timeScale.logicalToCoordinate(idxA as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
    const xB = timeScale.timeToCoordinate(data[idxB].time as UTCTimestamp) ??
               timeScale.logicalToCoordinate(idxB as unknown as Parameters<typeof timeScale.logicalToCoordinate>[0]);
    if (xA !== null && xB !== null && tB > tA) {
      return xA + ((time - tA) / (tB - tA)) * (xB - xA);
    }
    return xA ?? xB;
  }

  protected getY(price: number): number | null {
    if (!this.series) return null;
    return this.series.priceToCoordinate(price);
  }

  protected point(time: number, price: number): Point | null {
    const x = this.getX(time);
    const y = this.getY(price);
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
    const xA = this.getX(this.o.start_time);
    const xB = this.getX(this.o.end_time);
    const yA = this.getY(this.o.start_price);
    const yB = this.getY(this.o.end_price);
    if (xA === null || xB === null || yA === null || yB === null) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      ctx.save();
      const faded = !!(this.o as { isHistorical?: boolean }).isHistorical;
      ctx.globalAlpha = faded ? 0.25 : 1.0;
      ctx.strokeStyle = this.o.color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(this.o.style === "dashed" ? [6, 4] : this.o.style === "dotted" ? [2, 3] : []);
      ctx.beginPath();
      ctx.moveTo(xA, yA);
      ctx.lineTo(xB, yB);
      ctx.stroke();
      if (this.o.label) {
        ctx.setLineDash([]);
        ctx.fillStyle = this.o.color;
        ctx.font = "11px sans-serif";
        ctx.fillText(this.o.label, Math.max(xA, xB) + 4, (yA + yB) / 2 - 4);
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
    const xA = this.getX(this.o.start_time);
    const xB = this.getX(this.o.end_time);
    const yTop = this.getY(this.o.top);
    const yBottom = this.getY(this.o.bottom);
    if (xA === null || xB === null || yTop === null || yBottom === null) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      const x = Math.min(xA, xB);
      const y = Math.min(yTop, yBottom);
      const w = Math.max(2, Math.abs(xB - xA));
      const h = Math.max(2, Math.abs(yBottom - yTop));
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
