/**
 * Pure geometry of the drawing tools added after the lines (docs/decisions-drawing-tools.md):
 * vertical line, text, rectangle, Fibonacci, measure and parallel channel. Nothing here
 * touches the chart or the DOM; the controller paints and handles the mouse.
 *
 * Slopes are measured on the instrument's trading clock like a line's (SPEC section 9).
 * Only the channel has one: its second line is the first moved by a fixed price offset,
 * which keeps the lines parallel in trading time. The rules of `shapeError` are also
 * implemented by the server (`openchart.drawing_rules`); `shape-rules.fixture.json` is
 * run by both.
 */

import { POINTS_NEEDED, type DrawingType, type LineType, type Point, type TimeIndex } from "./geometry";
import type { Clock } from "./tradingTime";

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

const SAME = (a: Point, b: Point, clock: Clock) => clock(a.t) === clock(b.t);

/** Why these anchors are no `type`, or null. `label` matters for text only. */
export function shapeError(type: DrawingType, pts: Point[], clock: Clock, label?: string | null): string | null {
  const err = geometryError(type, pts, clock);
  if (err) return err;
  if (type === "text" && !(label ?? "").trim()) return "a text needs its text";
  return null;
}

function geometryError(type: DrawingType, pts: Point[], clock: Clock): string | null {
  if (pts.length !== POINTS_NEEDED[type]) return `a ${type} needs ${POINTS_NEEDED[type]} point(s)`;
  switch (type) {
    case "trendline":
    case "ray":
    case "extended":
      return SAME(pts[0], pts[1], clock) ? "the two points need trading time between them" : null;
    case "rect":
    case "fib":
      if (SAME(pts[0], pts[1], clock)) return "the two corners need trading time between them";
      return pts[0].p === pts[1].p ? "the two corners need different prices" : null;
    case "measure":
      return SAME(pts[0], pts[1], clock) && pts[0].p === pts[1].p ? "the two points must differ" : null;
    case "channel":
      if (SAME(pts[0], pts[1], clock)) return "the base line needs trading time between its two points";
      return Math.abs(channelOffset(pts, clock)) < 1e-9 ? "the third point must not be on the base line" : null;
    default:
      return null;
  }
}

// Channel

/** Price of the base line through `a` and `b`, extended to every time, at time `t`. */
function extendedPrice(a: Point, b: Point, t: number, clock: Clock): number {
  return a.p + ((b.p - a.p) * (clock(t) - clock(a.t))) / (clock(b.t) - clock(a.t));
}

/** Distance in price of the third anchor from the base line (extended, in trading time). */
export function channelOffset(pts: Point[], clock: Clock): number {
  return pts[2].p - extendedPrice(pts[0], pts[1], pts[2].t, clock);
}

/** The channel's two lines, as ordinary lines: the base and the base moved by the offset. */
export function channelLines(pts: Point[], clock: Clock, extend: boolean): { type: LineType; pts: Point[] }[] {
  const d = channelOffset(pts, clock);
  const type: LineType = extend ? "ray" : "trendline";
  const base = [pts[0], pts[1]];
  return [
    { type, pts: base },
    { type, pts: base.map((q) => ({ t: q.t, p: q.p + d })) },
  ];
}

// Fibonacci

/** Price of each level: 0 at the end anchor, 1 at the start anchor. */
export function fibLevels(pts: Point[]): { level: number; price: number }[] {
  const [a, b] = pts;
  return FIB_LEVELS.map((level) => ({ level, price: b.p + (a.p - b.p) * level }));
}

// Rectangle

export function rectBox(pts: Point[]): { t0: number; t1: number; p0: number; p1: number } {
  const [a, b] = pts;
  return { t0: Math.min(a.t, b.t), t1: Math.max(a.t, b.t), p0: Math.min(a.p, b.p), p1: Math.max(a.p, b.p) };
}

/** The four corner handles: anchor 1, the corner sharing its price, anchor 2, the other corner. */
export function rectCorners(pts: Point[]): Point[] {
  const [a, b] = pts;
  return [a, { t: b.t, p: a.p }, b, { t: a.t, p: b.p }];
}

// Moving

/** The anchors after dragging handle `handle` to `to`; null when the result breaks a rule. */
export function dragHandle(type: DrawingType, pts: Point[], handle: number, to: Point, clock: Clock): Point[] | null {
  let next: Point[];
  switch (type) {
    case "hline":
      next = [{ t: pts[0].t, p: to.p }];
      break;
    case "vline":
      next = [{ t: to.t, p: pts[0].p }];
      break;
    case "text":
      next = [to];
      break;
    case "rect": {
      // A corner takes its time from one anchor and its price from the other.
      const [a, b] = pts;
      const byHandle: Point[][] = [
        [to, b],
        [{ t: a.t, p: to.p }, { t: to.t, p: b.p }],
        [a, to],
        [{ t: to.t, p: a.p }, { t: b.t, p: to.p }],
      ];
      next = byHandle[handle];
      break;
    }
    case "channel": {
      if (handle === 2) {
        next = [pts[0], pts[1], to];
        break;
      }
      const d = channelOffset(pts, clock);
      const base = pts.slice(0, 2).map((q, i) => (i === handle ? to : q));
      const t3 = pts[2].t;
      next = [...base, { t: t3, p: extendedPrice(base[0], base[1], t3, clock) + d }];
      break;
    }
    default:
      next = pts.map((q, i) => (i === handle ? to : q));
  }
  return geometryError(type, next, clock) ? null : next;
}

/** All anchors moved by whole bars and a price; a vertical line only in time, a horizontal one only in price. */
export function moveShape(
  type: DrawingType,
  original: Point[],
  dBars: number,
  dPrice: number,
  index: TimeIndex,
  round: (price: number) => number,
): Point[] {
  return original.map((q) => ({
    t: type === "hline" ? q.t : index.toTime(Math.round(index.toLogical(q.t)) + dBars),
    p: type === "vline" ? q.p : round(q.p + dPrice),
  }));
}

// Measure

export interface MeasureStats {
  change: number;
  percent: number;
  bars: number;
  seconds: number;
  pips: number | null;
}

export function measureStats(pts: Point[], index: TimeIndex, pipSize?: number): MeasureStats {
  const [a, b] = pts;
  const change = b.p - a.p;
  return {
    change,
    percent: (change / a.p) * 100,
    bars: Math.round(index.toLogical(b.t) - index.toLogical(a.t)),
    seconds: b.t - a.t,
    pips: pipSize ? change / pipSize : null,
  };
}

/** "59s", "1m", "1h 30m", "2d 3h": the two largest units, sign dropped. */
export function formatDuration(seconds: number): string {
  let s = Math.round(Math.abs(seconds));
  const units: [string, number][] = [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
    ["s", 1],
  ];
  const parts: string[] = [];
  for (const [name, size] of units) {
    const n = Math.floor(s / size);
    s -= n * size;
    if (n > 0) parts.push(`${n}${name}`);
    if (parts.length === 2) break;
  }
  return parts.length ? parts.join(" ") : "0s";
}

// Style

/** "#rrggbb" and an opacity in percent as a CSS colour. */
export function withAlpha(hex: string, percent: number): string {
  const n = parseInt(hex.slice(1), 16);
  const alpha = Math.min(100, Math.max(0, percent)) / 100;
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** Text size in pixels for the style width 1-4. */
export function textSize(width: number): number {
  return [12, 14, 18, 24][Math.min(4, Math.max(1, Math.round(width))) - 1];
}

// Per-type options

const LINE_TYPE_LIST: readonly DrawingType[] = ["hline", "trendline", "ray", "extended"];

/** A line an alert can follow (the new tools have no alerts yet). */
export function isLineType(type: DrawingType): type is LineType {
  return LINE_TYPE_LIST.includes(type);
}

/** Which style controls apply to a type. `fill` and `extend` are the optional style keys. */
export interface StyleOptions {
  dash: boolean;
  fill: boolean;
  extend: boolean;
  label: boolean;
}

export function styleOptions(type: DrawingType): StyleOptions {
  return {
    dash: type !== "text",
    fill: type === "rect" || type === "channel",
    extend: type === "channel" || type === "fib",
    label: type !== "fib" && type !== "measure",
  };
}

/** The area opacity in percent: the stored `fill`, else the tool's default. */
export function fillOf(type: DrawingType, fill: number | undefined): number {
  if (fill !== undefined) return fill;
  return type === "rect" ? 15 : type === "channel" ? 10 : 0;
}

/** Black or white, whichever reads better on the colour `hex` ("#rrggbb"). */
export function contrastColor(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  const lum = (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  return lum > 0.6 ? "#111111" : "#ffffff";
}
