/**
 * Two Lightweight Charts series primitives for the indicator output kinds the library has no
 * series for: a band between two lines (`fill`) and tinted columns behind bars
 * (`background`). Like the drawing layer's, they hold their data and repaint when the
 * chart asks; each paints only the points on screen.
 */

import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

import type { FillData } from "./indicatorOutputs";

interface MediaTarget {
  useMediaCoordinateSpace<T>(f: (scope: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => T): T;
}

/** First and last index (end exclusive) of the points inside `[from, to]`, plus one each side. */
export function sliceAround(times: ArrayLike<number>, from: number, to: number): [number, number] {
  const n = times.length;
  let lo = 0;
  let hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < from) lo = mid + 1;
    else hi = mid;
  }
  const start = Math.max(0, lo - 1);
  hi = n;
  let l2 = lo;
  while (l2 < hi) {
    const mid = (l2 + hi) >> 1;
    if (times[mid] <= to) l2 = mid + 1;
    else hi = mid;
  }
  return [start, Math.min(n, l2 + 1)];
}

/** `[start, end)` stretches of `[from, to)` where both sides of the band have a value. */
export function fillRuns(upper: Float64Array, lower: Float64Array, from: number, to: number): [number, number][] {
  const runs: [number, number][] = [];
  let start = -1;
  for (let i = from; i < to; i++) {
    const ok = !Number.isNaN(upper[i]) && !Number.isNaN(lower[i]);
    if (ok && start < 0) start = i;
    else if (!ok && start >= 0) {
      runs.push([start, i]);
      start = -1;
    }
  }
  if (start >= 0) runs.push([start, to]);
  return runs;
}

/** Left and right edge of a column centred on `x`; neighbours share an edge, and none is thinner than a pixel. */
export function columnEdges(x: number, spacing: number): [number, number] {
  const left = Math.round(x - spacing / 2);
  return [left, Math.max(left + 1, Math.round(x + spacing / 2))];
}

/** The rectangles for columns centred on `xs` (ascending): columns that touch become one. */
export function backgroundRuns(xs: readonly number[], spacing: number): [number, number][] {
  const runs: [number, number][] = [];
  for (const x of xs) {
    const [left, right] = columnEdges(x, spacing);
    const last = runs[runs.length - 1];
    if (last && left <= last[1]) last[1] = Math.max(last[1], right);
    else runs.push([left, right]);
  }
  return runs;
}

type Attached = SeriesAttachedParameter<Time, SeriesType>;

/** The part both primitives share: what to hold, when to repaint, where to draw. */
abstract class Base implements ISeriesPrimitive<Time> {
  protected params?: Attached;
  protected visible = true;

  attached(params: Attached): void {
    this.params = params;
  }

  detached(): void {
    this.params = undefined;
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.params?.requestUpdate();
  }

  protected abstract draw(context: CanvasRenderingContext2D, size: { width: number; height: number }): void;

  /** Behind the series, so lines and candles stay on top of a band or a tint. */
  paneViews(): readonly IPrimitivePaneView[] {
    const renderer: IPrimitivePaneRenderer = {
      draw: () => {},
      drawBackground: (target: unknown) => {
        if (this.visible && this.params) (target as MediaTarget).useMediaCoordinateSpace(({ context, mediaSize }) => this.draw(context, mediaSize));
      },
    };
    const view: IPrimitivePaneView = { zOrder: () => "bottom", renderer: () => renderer };
    return [view];
  }

  updateAllViews(): void {}

  protected visibleTimes(): { from: number; to: number } | undefined {
    const r = this.params?.chart.timeScale().getVisibleRange();
    return r ? { from: Number(r.from), to: Number(r.to) } : undefined;
  }
}

/** The band between two lines, painted with `color` (already carrying its opacity). */
export class FillPrimitive extends Base {
  private data: FillData = { time: [], upper: new Float64Array(0), lower: new Float64Array(0) };
  private color = "rgba(0, 0, 0, 0.1)";

  /** The series whose price scale the two lines are on (the upper line's own series). */
  constructor(private readonly series: ISeriesApi<SeriesType>) {
    super();
  }

  setData(data: FillData): void {
    this.data = data;
    this.params?.requestUpdate();
  }

  setColor(color: string): void {
    this.color = color;
    this.params?.requestUpdate();
  }

  protected draw(context: CanvasRenderingContext2D): void {
    const range = this.visibleTimes();
    const chart = this.params?.chart;
    if (!range || !chart) return;
    const { time, upper, lower } = this.data;
    const [lo, hi] = sliceAround(time, range.from, range.to);
    const scale = chart.timeScale();
    context.fillStyle = this.color;
    for (const [start, end] of fillRuns(upper, lower, lo, hi)) {
      const xs: number[] = [];
      const top: number[] = [];
      const bottom: number[] = [];
      for (let i = start; i < end; i++) {
        const x = scale.timeToCoordinate(time[i] as Time);
        const y1 = this.series.priceToCoordinate(upper[i]);
        const y2 = this.series.priceToCoordinate(lower[i]);
        if (x === null || y1 === null || y2 === null) continue;
        xs.push(x);
        top.push(y1);
        bottom.push(y2);
      }
      if (xs.length < 2) continue;
      context.beginPath();
      context.moveTo(xs[0], top[0]);
      for (let i = 1; i < xs.length; i++) context.lineTo(xs[i], top[i]);
      for (let i = xs.length - 1; i >= 0; i--) context.lineTo(xs[i], bottom[i]);
      context.closePath();
      context.fill();
    }
  }
}

/** Full-height tinted columns on the bars at `times` (chart times, ascending). */
export class BackgroundPrimitive extends Base {
  private times: number[] = [];
  private color = "rgba(0, 0, 0, 0.1)";

  setTimes(times: number[]): void {
    this.times = times;
    this.params?.requestUpdate();
  }

  setColor(color: string): void {
    this.color = color;
    this.params?.requestUpdate();
  }

  protected draw(context: CanvasRenderingContext2D, size: { width: number; height: number }): void {
    const range = this.visibleTimes();
    const scale = this.params?.chart.timeScale();
    if (!range || !scale) return;
    const x0 = scale.logicalToCoordinate(0 as never);
    const x1 = scale.logicalToCoordinate(1 as never);
    if (x0 === null || x1 === null) return;
    const spacing = Math.abs(x1 - x0);
    const [lo, hi] = sliceAround(this.times, range.from, range.to);
    const xs: number[] = [];
    for (let i = lo; i < hi; i++) {
      const x = scale.timeToCoordinate(this.times[i] as Time);
      if (x !== null) xs.push(x);
    }
    context.fillStyle = this.color;
    for (const [left, right] of backgroundRuns(xs, spacing)) {
      if (right < 0 || left > size.width) continue;
      context.fillRect(left, 0, right - left, size.height);
    }
  }
}
