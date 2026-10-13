/**
 * Drawing geometry (SPEC section 9, "Line geometry").
 *
 * A line is defined by two anchors (t: UTC epoch seconds, p: price). Its slope is
 * measured on the instrument's trading clock T (tradingTime.ts), which only runs while
 * the market is open:
 *   line_price(t) = p1 + (p2 - p1) * (T(t) - T(t1)) / (T(t2) - T(t1))
 * so a line keeps its slope across weekends and breaks, looks straight on the chart,
 * and is exactly the line the alert engine checks (openchart.alerts.geometry).
 *
 * The chart's x axis is a bar index ("logical"). `TimeIndex` maps time to a fractional
 * bar index and back, linearly inside each bar's slot. Lines are drawn through one
 * vertex per bar, which keeps them exact even where a bar holds a closure.
 */

import type { Clock } from "./tradingTime";

const clockTime: Clock = (t) => t;

export type DrawingType = "hline" | "trendline" | "ray" | "extended";

export interface Point {
  t: number;
  p: number;
}

export const POINTS_NEEDED: Record<DrawingType, number> = { hline: 1, trendline: 2, ray: 2, extended: 2 };

/** Price of the drawing at clock time `t`, or null where the drawing does not exist. */
export function linePrice(type: DrawingType, pts: Point[], t: number, clock: Clock = clockTime): number | null {
  if (type === "hline") return pts[0].p;
  const [from, to] = timeRange(type, pts);
  if (!(t >= from && t <= to)) return null;
  const [a, b] = pts;
  const span = clock(b.t) - clock(a.t);
  if (span === 0) return null;
  return a.p + ((b.p - a.p) * (clock(t) - clock(a.t))) / span;
}

/** Clock-time interval where the drawing exists (may be infinite). */
export function timeRange(type: DrawingType, pts: Point[]): [number, number] {
  if (type === "hline" || type === "extended") return [-Infinity, Infinity];
  const [a, b] = pts;
  if (type === "trendline") return [Math.min(a.t, b.t), Math.max(a.t, b.t)];
  // A ray starts at point 1 and runs through point 2 to infinity.
  return b.t >= a.t ? [a.t, Infinity] : [-Infinity, a.t];
}

/** Clock time <-> fractional bar index, over the bar start times of the chart. */
export class TimeIndex {
  constructor(
    private readonly ts: readonly number[],
    private readonly step: number,
  ) {}

  get size(): number {
    return this.ts.length;
  }

  toLogical(t: number): number {
    const ts = this.ts;
    const n = ts.length;
    if (n === 0) return NaN;
    if (t <= ts[0]) return (t - ts[0]) / this.step;
    if (t >= ts[n - 1]) return n - 1 + (t - ts[n - 1]) / this.step;
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= t) lo = mid;
      else hi = mid;
    }
    return lo + (t - ts[lo]) / (ts[lo + 1] - ts[lo]);
  }

  toTime(logical: number): number {
    const ts = this.ts;
    const n = ts.length;
    if (n === 0) return NaN;
    if (logical <= 0) return ts[0] + logical * this.step;
    if (logical >= n - 1) return ts[n - 1] + (logical - (n - 1)) * this.step;
    const i = Math.floor(logical);
    return ts[i] + (logical - i) * (ts[i + 1] - ts[i]);
  }

  /** Bar start time of the bar nearest to `logical` (whole bars only). */
  snapTime(logical: number): number {
    return this.toTime(Math.round(logical));
  }
}

export interface Vertex {
  logical: number;
  t: number;
  price: number;
}

/**
 * Vertices of a line between bar indexes `from` and `to` (the visible range): its
 * start and end, plus one at every whole bar in between. Straight segments between
 * them are exact, because time and price are both linear inside one bar's slot.
 */
export function lineVertices(
  type: DrawingType,
  pts: Point[],
  index: TimeIndex,
  from: number,
  to: number,
  clock: Clock = clockTime,
): Vertex[] {
  if (index.size === 0) return [];
  const [t0, t1] = timeRange(type, pts);
  const start = Math.max(from, Number.isFinite(t0) ? index.toLogical(t0) : -Infinity);
  const end = Math.min(to, Number.isFinite(t1) ? index.toLogical(t1) : Infinity);
  if (!(end >= start)) return [];
  const out: Vertex[] = [];
  const add = (logical: number) => {
    const t = index.toTime(logical);
    const price = linePrice(type, pts, Math.min(Math.max(t, t0), t1), clock);
    if (price !== null) out.push({ logical, t, price });
  };
  add(start);
  for (let l = Math.floor(start) + 1; l < end; l++) add(l);
  if (end > start) add(end);
  return out;
}

/**
 * Screen x of a fractional bar index, from the chart's x of whole indexes.
 *
 * Lightweight Charts only maps whole bar indexes (5.2.1 returns 0 for 999.5), but a
 * line's ends often fall inside a bar: a point placed on a lower timeframe, or the
 * edge of the visible range. Bars are evenly spaced, so interpolate.
 */
export function logicalX(logical: number, wholeX: (index: number) => number | null): number | null {
  const i = Math.floor(logical);
  const x0 = wholeX(i);
  if (x0 === null || logical === i) return x0;
  const x1 = wholeX(i + 1);
  return x1 === null ? null : x0 + (logical - i) * (x1 - x0);
}

/** Distance from (px, py) to the segment (ax, ay)-(bx, by). */
export function segmentDistance(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  const u = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
  return Math.hypot(px - (ax + u * dx), py - (ay + u * dy));
}

/** The bar's open, high, low or close nearest to `price` (Ctrl magnet). */
export function nearestOhlc(bar: { o: number; h: number; l: number; c: number }, price: number): number {
  let best = bar.o;
  for (const v of [bar.h, bar.l, bar.c]) if (Math.abs(v - price) < Math.abs(best - price)) best = v;
  return best;
}
