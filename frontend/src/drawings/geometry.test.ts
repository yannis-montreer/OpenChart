import { describe, expect, it } from "vitest";
import { TimeIndex, linePath, lineVertices, linePrice, logicalX, nearestOhlc, nextOpenSlot, segmentDistance, timeRange, wallAdvance } from "./geometry";

const A = { t: 1000, p: 10 };
const B = { t: 2000, p: 20 };

describe("linePrice (SPEC section 9)", () => {
  it("follows the two-point formula in clock time", () => {
    expect(linePrice("extended", [A, B], 1500)).toBe(15);
    expect(linePrice("extended", [A, B], 3000)).toBe(30);
    expect(linePrice("extended", [A, B], 0)).toBe(0);
  });

  it("limits a trendline to its two points", () => {
    expect(linePrice("trendline", [A, B], 1000)).toBe(10);
    expect(linePrice("trendline", [A, B], 2000)).toBe(20);
    expect(linePrice("trendline", [A, B], 2001)).toBeNull();
    expect(linePrice("trendline", [B, A], 999)).toBeNull(); // either order
  });

  it("starts a ray at point 1 and runs through point 2", () => {
    expect(linePrice("ray", [A, B], 999)).toBeNull();
    expect(linePrice("ray", [A, B], 5000)).toBe(50);
    // Point 2 to the left of point 1: the ray runs to the left.
    expect(linePrice("ray", [B, A], 2001)).toBeNull();
    expect(linePrice("ray", [B, A], 0)).toBe(0);
    expect(timeRange("ray", [B, A])).toEqual([-Infinity, 2000]);
  });

  it("keeps a horizontal line flat everywhere", () => {
    expect(linePrice("hline", [A], -1e9)).toBe(10);
    expect(linePrice("hline", [A], 1e12)).toBe(10);
  });
});

// Bars at 0, 60, 120, then a weekend-like gap, then 3600, 3660.
const TS = [0, 60, 120, 3600, 3660];
const index = new TimeIndex(TS, 60);

describe("TimeIndex", () => {
  it("maps bar times to whole indexes and back", () => {
    TS.forEach((t, i) => {
      expect(index.toLogical(t)).toBe(i);
      expect(index.toTime(i)).toBe(t);
    });
  });

  it("is linear inside a bar's slot, including across a gap", () => {
    expect(index.toLogical(30)).toBe(0.5);
    expect(index.toLogical(1860)).toBe(2.5); // halfway through the gap
    expect(index.toTime(2.5)).toBe(1860);
  });

  it("extrapolates with the timeframe step beyond the data", () => {
    expect(index.toLogical(3660 + 600)).toBe(14);
    expect(index.toTime(14)).toBe(4260);
    expect(index.toLogical(-120)).toBe(-2);
    expect(index.snapTime(2.6)).toBe(3600);
  });

  it("round-trips any time", () => {
    for (const t of [-500, 0, 17, 100, 2000, 3600, 3700, 99999]) {
      expect(index.toTime(index.toLogical(t))).toBeCloseTo(t, 9);
    }
  });
});

describe("lineVertices", () => {
  it("puts a vertex on every bar and stays on the clock-time line", () => {
    const pts = [
      { t: 0, p: 100 },
      { t: 60, p: 99 },
    ];
    const vs = lineVertices("ray", pts, index, -5, 6);
    expect(vs[0].logical).toBe(0); // the ray starts at point 1
    expect(vs.map((v) => v.logical)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    for (const v of vs) expect(v.price).toBeCloseTo(100 - v.t / 60, 9);
    // Across the gap (bar 2 -> 3) the price drops by the whole gap's worth.
    expect(vs[3].price - vs[2].price).toBeCloseTo(-(3600 - 120) / 60, 9);
  });

  it("clips a trendline to its points and to the visible range", () => {
    const pts = [
      { t: 60, p: 1 },
      { t: 3600, p: 2 },
    ];
    expect(lineVertices("trendline", pts, index, -10, 10).map((v) => v.logical)).toEqual([1, 2, 3]);
    expect(lineVertices("trendline", pts, index, 1.5, 2.5).map((v) => v.logical)).toEqual([1.5, 2, 2.5]);
    expect(lineVertices("trendline", pts, index, 4, 9)).toEqual([]);
  });
});

describe("helpers", () => {
  it("places a point inside a bar between the bar's neighbours", () => {
    // Like Lightweight Charts: whole indexes only, anything else maps to 0.
    const wholeX = (i: number) => (Number.isInteger(i) ? 100 + 7 * i : 0);
    expect(logicalX(999, wholeX)).toBe(7093);
    expect(logicalX(999.25, wholeX)).toBeCloseTo(7094.75);
    expect(logicalX(-2.5, wholeX)).toBeCloseTo(82.5);
    expect(logicalX(3.5, (i) => (i > 3 ? null : 0))).toBeNull();
  });

  it("measures distance to a segment", () => {
    expect(segmentDistance(5, 5, 0, 0, 10, 0)).toBe(5);
    expect(segmentDistance(-3, 4, 0, 0, 10, 0)).toBe(5);
  });

  it("finds the nearest of open, high, low and close", () => {
    const bar = { o: 10, h: 15, l: 8, c: 12 };
    expect(nearestOhlc(bar, 14)).toBe(15);
    expect(nearestOhlc(bar, 8.7)).toBe(8);
    expect(nearestOhlc(bar, 11.2)).toBe(12);
    expect(nearestOhlc(bar, 10.4)).toBe(10);
  });
});

import fixture from "./trading-time.fixture.json";
import { tradingClock, type Closure, type TradingHoursJson } from "./tradingTime";

describe("trading clock (same as the Python server)", () => {
  type Case = { hours: TradingHoursJson; closures: Closure[]; samples: [number, number][] };
  for (const [name, data] of Object.entries(fixture as unknown as Record<string, Case>)) {
    it(`matches the server for ${name} hours`, () => {
      const clock = tradingClock(data.hours, data.closures);
      for (const [t, expected] of data.samples) expect(clock(t)).toBe(expected);
    });
  }

  it("keeps a line straight across the weekend", () => {
    const gold = tradingClock(fixture.metal.hours as unknown as TradingHoursJson);
    const utc = (d: number, h: number) => Date.UTC(2024, 6, d, h) / 1000;
    const pts = [
      { t: utc(11, 13), p: 100 }, // Thursday 09:00 New York
      { t: utc(12, 13), p: 99 }, // Friday: -1 per trading day
    ];
    expect(linePrice("extended", pts, utc(15, 13), gold)).toBeCloseTo(98, 9); // Monday
    expect(linePrice("extended", pts, utc(15, 13))).toBeCloseTo(96, 9); // clock time
  });
});

// The empty area to the right of the last bar (owner's bug report, 2026-10-13)

const GOLD = fixture.metal.hours as unknown as TradingHoursJson;
const FX = fixture.fx.hours as unknown as TradingHoursJson;
const at = (m: number, d: number, h: number) => Date.UTC(2024, m - 1, d, h) / 1000;

/** Bars up to `last`, `step` apart (closed slots included: only the shape matters). */
function bars(last: number, step: number, count = 50): number[] {
  return Array.from({ length: count }, (_, i) => last - (count - 1 - i) * step);
}

/** Lightweight Charts 5.2.1: whole indexes only, anything else maps to x = 0. */
const chartX = (i: number) => (Number.isInteger(i) ? 50 + 10 * i : 0);
const x = (l: number) => logicalX(l, chartX);
const y = (p: number) => 1000 - p;

describe("future slots skip closed periods", () => {
  it("finds the next slot with open time", () => {
    const gold = tradingClock(GOLD);
    const fri = at(7, 12, 17); // Friday 13:00 New York: open
    expect(nextOpenSlot(fri, 4 * 3600, gold)).toBe(fri);
    // Friday 17:00 New York (21:00 UTC): closed until Sunday 18:00, in the 17:00 slot.
    expect(nextOpenSlot(at(7, 12, 21), 4 * 3600, gold)).toBe(at(7, 14, 21));
    expect(nextOpenSlot(at(7, 12, 21), 86400, gold)).toBe(at(7, 14, 21));
  });

  it("places 4h and 1D slots on trading days only", () => {
    const gold = tradingClock(GOLD);
    const h4 = new TimeIndex(bars(at(7, 12, 13), 4 * 3600), 4 * 3600, gold); // last: Fri 09:00 NY
    expect(h4.toTime(50)).toBe(at(7, 12, 17));
    expect(h4.toTime(51)).toBe(at(7, 14, 21)); // Sunday 17:00 New York, not Friday night
    expect(h4.toLogical(at(7, 14, 21))).toBe(51);
    expect(h4.toLogical(at(7, 14, 23))).toBeCloseTo(51 + 1 / 3, 9); // 1 of its 3 open hours
    const d1 = new TimeIndex(bars(at(7, 11, 21), 86400), 86400, gold); // last: Thursday's day
    expect(d1.toTime(50)).toBe(at(7, 14, 21)); // Friday's day is followed by Monday's
    for (const t of [at(7, 16, 5), at(7, 23, 3), at(8, 30, 12)]) expect(d1.toTime(d1.toLogical(t))).toBeCloseTo(t, 3);
    // Saturday has no trading time of its own: it sits where Monday's trading day starts.
    expect(d1.toLogical(at(7, 13, 5))).toBe(50);
  });
});

describe("a line in the empty area", () => {
  function screen(hours: TradingHoursJson, last: number, step: number, a: number, b: number) {
    const clock = tradingClock(hours);
    const index = new TimeIndex(bars(last, step), step, clock);
    const pts = [
      { t: a, p: 100 },
      { t: b, p: 90 },
    ];
    // A fractional visible range, as after any scroll or zoom.
    return { index, path: linePath("ray", pts, index, 30.37, 120.61, clock, x, y) };
  }

  it("draws one polyline from left to right: no line back to the left edge", () => {
    const last = at(7, 10, 13);
    const step = 4 * 3600;
    // Point 2 beyond the last bar, like the dragged anchor in the report.
    const { index, path } = screen(GOLD, last, step, last - 10 * step, last + 20 * step);
    expect(index.toLogical(last + 20 * step)).toBeGreaterThan(49);
    expect(path.length).toBeGreaterThan(50);
    for (let i = 1; i < path.length; i++) expect(path[i][0]).toBeGreaterThan(path[i - 1][0]);
    expect(Math.min(...path.map(([px]) => px))).toBeGreaterThan(chartX(30)); // never x = 0
  });

  it("keeps a constant slope on 1D across weekends", () => {
    const last = at(7, 10, 21); // Wednesday 17:00 New York
    const { path } = screen(GOLD, last, 86400, last - 5 * 86400, last + 4 * 86400);
    const future = path.filter(([px]) => px > chartX(49));
    expect(future.length).toBeGreaterThan(40); // several weekends
    const slopes = future.slice(1).map(([px, py], i) => (py - future[i][1]) / (px - future[i][0]));
    for (const s of slopes) expect(s).toBeCloseTo(slopes[0], 6);
  });

  it("on EUR/USD 4h, only the 17:00 bar moves a little less (the 5-minute rollover)", () => {
    const last = at(7, 10, 13);
    const step = 4 * 3600;
    const { path } = screen(FX, last, step, last - 10 * step, last + 6 * step);
    const future = path.filter(([px]) => px > chartX(49));
    const slopes = future.slice(1).map(([px, py], i) => (py - future[i][1]) / (px - future[i][0]));
    expect(slopes.length).toBeGreaterThan(60);
    const full = Math.max(...slopes);
    expect(slopes.filter((d) => Math.abs(d / full - 235 / 240) < 1e-6).length).toBeGreaterThan(5);
    for (const d of slopes) expect([1, 235 / 240].some((r) => Math.abs(d / full - r) < 1e-6)).toBe(true);
  });

  it("on EUR/USD 1m, a ray runs straight across the 17:00 New York rollover", () => {
    // Last bar 16:50 New York (20:50 UTC, summer); future minute slots skip 17:00-17:04.
    const fx = tradingClock(FX);
    const last = Date.UTC(2024, 6, 10, 20, 50) / 1000;
    const index = new TimeIndex(bars(last, 60), 60, fx);
    expect(index.toTime(49 + 9)).toBe(Date.UTC(2024, 6, 10, 20, 59) / 1000);
    expect(index.toTime(49 + 10)).toBe(Date.UTC(2024, 6, 10, 21, 5) / 1000); // not 17:00
    const pts = [
      { t: last - 30 * 60, p: 100 },
      { t: last, p: 97 },
    ];
    const path = linePath("ray", pts, index, 30.4, 80.2, fx, x, y);
    const future = path.filter(([px]) => px > chartX(49));
    const slopes = future.slice(1).map(([px, py], i) => (py - future[i][1]) / (px - future[i][0]));
    expect(slopes.length).toBeGreaterThan(20);
    for (const s of slopes) expect(s).toBeCloseTo(slopes[0], 6);
  });

  it("on gold 4h, a weekend looks like any other night (no flat steps)", () => {
    // Gold's 4h bar that holds the daily break (17:00-21:00 New York) has 3 open
    // hours, so it moves the line 3/4 as far; the weekend reopen is that same bar.
    const last = at(7, 10, 13);
    const step = 4 * 3600;
    const { path } = screen(GOLD, last, step, last - 10 * step, last + 6 * step);
    const future = path.filter(([px]) => px > chartX(49));
    const slopes = future.slice(1).map(([px, py], i) => (py - future[i][1]) / (px - future[i][0]));
    const full = Math.max(...slopes);
    expect(slopes.filter((d) => Math.abs(d / full - 0.75) < 1e-6).length).toBeGreaterThan(5);
    for (const d of slopes) {
      expect(d).toBeGreaterThan(0); // the ray falls: every slot moves it, no flat steps
      expect([1, 0.75].some((r) => Math.abs(d / full - r) < 1e-6)).toBe(true);
    }
  });

  it("stays straight through a daylight saving change (slots on New York time)", () => {
    const gold = tradingClock(GOLD);
    const day = 86400;
    const last = at(10, 24, 21); // Thursday 17:00 New York (EDT)
    const index = new TimeIndex(bars(last, day), day, gold, wallAdvance(day, "America/New_York"));
    // After the change Monday's day starts Sunday 17:00 New York = 22:00 UTC (EST).
    expect(index.toTime(54)).toBe(at(10, 31, 21)); // Friday's day, EDT
    expect(index.toTime(55)).toBe(at(11, 3, 22));
    const pts = [
      { t: last - 3 * day, p: 100 },
      { t: last, p: 97 },
    ];
    const path = linePath("ray", pts, index, 30.4, 80.2, gold, x, y);
    const future = path.filter(([px]) => px > chartX(49));
    const slopes = future.slice(1).map(([px, py], i) => (py - future[i][1]) / (px - future[i][0]));
    for (const sl of slopes) expect(sl).toBeCloseTo(slopes[0], 6);
  });
});
