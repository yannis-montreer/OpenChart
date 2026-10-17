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
 * vertex per bar, which keeps them exact even where a bar holds a closure. Past the
 * last bar the slots are the bars still to come: closed periods are skipped, like real
 * bars, so a line keeps its slope on screen in the empty area as well.
 */

import { tzOffset } from "../time";
import type { Clock } from "./tradingTime";

const clockTime: Clock = (t) => t;

/** The types an alert can follow: lines with a price at every time they exist. */
export type LineType = "hline" | "trendline" | "ray" | "extended";

/** Tools added later (docs/decisions-drawing-tools.md); their geometry is in shapes.ts. */
export type ShapeType = "vline" | "text" | "rect" | "fib" | "measure" | "channel";

export type DrawingType = LineType | ShapeType;

export interface Point {
  t: number;
  p: number;
}

export const POINTS_NEEDED: Record<DrawingType, number> = {
  hline: 1,
  trendline: 2,
  ray: 2,
  extended: 2,
  vline: 1,
  text: 1,
  rect: 2,
  fib: 2,
  measure: 2,
  channel: 3,
};

/** Price of the drawing at clock time `t`, or null where the drawing does not exist. */
export function linePrice(type: LineType, pts: Point[], t: number, clock: Clock = clockTime): number | null {
  if (type === "hline") return pts[0].p;
  const [from, to] = timeRange(type, pts);
  if (!(t >= from && t <= to)) return null;
  const [a, b] = pts;
  const span = clock(b.t) - clock(a.t);
  if (span === 0) return null;
  return a.p + ((b.p - a.p) * (clock(t) - clock(a.t))) / span;
}

/** Clock-time interval where the drawing exists (may be infinite). */
export function timeRange(type: LineType, pts: Point[]): [number, number] {
  if (type === "hline" || type === "extended") return [-Infinity, Infinity];
  const [a, b] = pts;
  if (type === "trendline") return [Math.min(a.t, b.t), Math.max(a.t, b.t)];
  // A ray starts at point 1 and runs through point 2 to infinity.
  return b.t >= a.t ? [a.t, Infinity] : [-Infinity, a.t];
}

/** Future slots generated at most (on 1m: two weeks); beyond, plain steps. */
const MAX_FUTURE_SLOTS = 20_000;

/** Start of the next slot: `t + step`, or on a wall clock (see `wallAdvance`). */
export type Advance = (t: number) => number;

/**
 * `t + step` on the wall clock of `tz`: 4h, 1D and 1W bars start at 17:00 New York
 * also after a daylight saving change (a 1D step is then 23 or 25 hours).
 */
export function wallAdvance(step: number, tz: string): Advance {
  return (t) => {
    const local = t + tzOffset(t, tz) + step;
    const guess = local - tzOffset(t, tz);
    return local - tzOffset(guess, tz);
  };
}

/**
 * Start of the first slot at or after `start` (slots `step` long) with open time in
 * it, by the trading clock. With `advance` (wall-clock slots) it steps slot by slot;
 * otherwise it skips closed stretches with an exponential search.
 */
export function nextOpenSlot(start: number, step: number, clock: Clock, advance?: Advance): number {
  if (advance) {
    let s = start;
    for (let k = 0; k < 1000; k++) {
      const next = advance(s);
      if (clock(next) > clock(s)) return s;
      s = next;
    }
    return start; // never opens (no hours?): keep plain slots
  }
  const base = clock(start);
  if (clock(start + step) > base) return start;
  let hi = 2;
  while (clock(start + hi * step) === base) {
    if (hi > 1 << 22) return start; // never opens (no hours?): keep plain steps
    hi *= 2;
  }
  // The first open time lies in slot m, with lo < m + 1 <= hi.
  let lo = 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (clock(start + mid * step) === base) lo = mid;
    else hi = mid;
  }
  return start + lo * step;
}

/** Clock time <-> fractional bar index, over the bar start times of the chart. */
export class TimeIndex {
  /** Bar starts after the last bar: the slots of the bars still to come. */
  private readonly future: number[] = [];

  constructor(
    private readonly ts: readonly number[],
    private readonly step: number,
    private readonly clock?: Clock,
    private readonly advance?: Advance,
  ) {}

  get size(): number {
    return this.ts.length;
  }

  /** Start of slot `k` after the last bar (k >= 1), skipping closed periods. */
  private futureStart(k: number): number {
    const last = this.ts[this.ts.length - 1];
    if (k > MAX_FUTURE_SLOTS) return this.futureStart(MAX_FUTURE_SLOTS) + (k - MAX_FUTURE_SLOTS) * this.step;
    const future = this.future;
    while (future.length < k) {
      const prev = future.length ? future[future.length - 1] : last;
      const next = this.advance ? this.advance(prev) : prev + this.step;
      future.push(this.clock ? nextOpenSlot(next, this.step, this.clock, this.advance) : next);
    }
    return future[k - 1];
  }

  /** Start of whole bar index `i` (extrapolated before the first bar). */
  private start(i: number): number {
    const ts = this.ts;
    if (i < 0) return ts[0] + i * this.step;
    if (i < ts.length) return ts[i];
    return this.futureStart(i - (ts.length - 1));
  }

  toLogical(t: number): number {
    const ts = this.ts;
    const n = ts.length;
    if (n === 0) return NaN;
    if (t <= ts[0]) return (t - ts[0]) / this.step;
    if (t >= ts[n - 1]) {
      // Past the last bar: search the future slots (slot 0 is the last bar), generating
      // only as many as needed.
      let lo = 0;
      let hi = 1;
      while (hi < MAX_FUTURE_SLOTS && this.futureStart(hi) <= t) hi = Math.min(hi * 2, MAX_FUTURE_SLOTS);
      if (this.futureStart(hi) <= t) {
        return n - 1 + hi + (t - this.futureStart(hi)) / this.step; // beyond the generated slots
      }
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (this.start(n - 1 + mid) <= t) lo = mid;
        else hi = mid;
      }
      return n - 1 + lo + this.within(n - 1 + lo, t);
    }
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= t) lo = mid;
      else hi = mid;
    }
    return lo + this.within(lo, t);
  }

  /**
   * Position of `t` inside slot `i`, from 0 to 1. With a trading clock it is measured
   * in trading time, so a line is straight on screen at any position, also in a slot
   * that runs into a weekend or a closure.
   */
  private within(i: number, t: number): number {
    const a = this.start(i);
    const b = this.start(i + 1);
    if (this.clock) {
      const ta = this.clock(a);
      const tb = this.clock(b);
      if (tb > ta) return (this.clock(t) - ta) / (tb - ta);
    }
    return (t - a) / (b - a);
  }

  toTime(logical: number): number {
    const ts = this.ts;
    const n = ts.length;
    if (n === 0) return NaN;
    if (logical <= 0) return ts[0] + logical * this.step;
    const i = Math.floor(logical);
    const a = this.start(i);
    if (logical === i) return a;
    const b = this.start(i + 1);
    const f = logical - i;
    const clock = this.clock;
    if (clock) {
      const ta = clock(a);
      const tb = clock(b);
      if (tb > ta) {
        // The inverse of `within`: the first time with that much trading time.
        const target = ta + f * (tb - ta);
        let lo = a;
        let hi = b;
        for (let k = 0; k < 64 && hi - lo > 1e-6; k++) {
          const mid = (lo + hi) / 2;
          if (clock(mid) < target) lo = mid;
          else hi = mid;
        }
        return hi;
      }
    }
    return a + f * (b - a);
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
  type: LineType,
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
 * The screen polyline of a line between bar indexes `from` and `to`: its vertices,
 * mapped with `x` (bar index -> pixels) and `y` (price -> pixels). Vertices the chart
 * cannot place are left out.
 */
export function linePath(
  type: Exclude<LineType, "hline">,
  pts: Point[],
  index: TimeIndex,
  from: number,
  to: number,
  clock: Clock,
  x: (logical: number) => number | null,
  y: (price: number) => number | null,
): [number, number][] {
  const path: [number, number][] = [];
  for (const v of lineVertices(type, pts, index, from, to, clock)) {
    const px = x(v.logical);
    const py = y(v.price);
    if (px !== null && py !== null && Number.isFinite(px) && Number.isFinite(py)) path.push([px, py]);
  }
  return path;
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
