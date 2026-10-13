import { describe, expect, it } from "vitest";
import { TimeIndex, lineVertices, linePrice, nearestOhlc, segmentDistance, timeRange } from "./geometry";

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
import { tradingClock, type TradingHoursJson } from "./tradingTime";

describe("trading clock (same as the Python server)", () => {
  for (const [name, data] of Object.entries(fixture as unknown as Record<string, { hours: TradingHoursJson; samples: [number, number][] }>)) {
    it(`matches the server for ${name} hours`, () => {
      const clock = tradingClock(data.hours);
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
