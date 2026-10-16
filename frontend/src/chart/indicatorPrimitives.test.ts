import { describe, expect, it } from "vitest";

import { backgroundRuns, columnEdges, fillRuns, sliceAround } from "./indicatorPrimitives";

describe("which points are on screen", () => {
  const times = [10, 20, 30, 40, 50, 60, 70];

  it("takes those inside the range plus one more each side, so a line leaves the screen", () => {
    expect(sliceAround(times, 30, 50)).toEqual([1, 6]);
  });

  it("clips at the ends of the data", () => {
    expect(sliceAround(times, 0, 15)).toEqual([0, 2]);
    expect(sliceAround(times, 65, 100)).toEqual([5, 7]);
  });

  it("is everything when the range covers everything, nothing for no data", () => {
    expect(sliceAround(times, 0, 100)).toEqual([0, 7]);
    expect(sliceAround([], 0, 100)).toEqual([0, 0]);
  });

  it("finds its place in 100,000 points by halving, not by walking", () => {
    const big = Array.from({ length: 100_000 }, (_, i) => i * 60);
    expect(sliceAround(big, 3_000_000, 3_000_600)).toEqual([49_999, 50_012]);
  });
});

describe("runs of a band", () => {
  const nan = Number.NaN;

  it("are the stretches where both sides have a value", () => {
    const upper = Float64Array.from([1, 2, nan, 4, 5, 6, nan]);
    const lower = Float64Array.from([0, 1, nan, 3, 4, nan, nan]);
    expect(fillRuns(upper, lower, 0, 7)).toEqual([
      [0, 2],
      [3, 5],
    ]);
  });

  it("respects the slice it is given and has no run for a lone point's absence", () => {
    const upper = Float64Array.from([1, 2, 3, 4]);
    const lower = Float64Array.from([0, 1, 2, 3]);
    expect(fillRuns(upper, lower, 1, 3)).toEqual([[1, 3]]);
    expect(fillRuns(Float64Array.from([nan]), Float64Array.from([nan]), 0, 1)).toEqual([]);
  });

  it("keeps a run of one point (it is drawn as nothing, not dropped, so a neighbour can join)", () => {
    expect(fillRuns(Float64Array.from([1, nan]), Float64Array.from([0, nan]), 0, 2)).toEqual([[0, 1]]);
  });
});

describe("background columns", () => {
  it("are centred on the bar and as wide as the bar spacing", () => {
    expect(columnEdges(100, 10)).toEqual([95, 105]);
  });

  it("abut: one bar's right edge is the next bar's left edge, at any fractional spacing", () => {
    for (const spacing of [3.3, 6.5, 7, 12.25]) {
      for (let i = 0; i < 50; i++) {
        const x = 20.4 + i * spacing;
        expect(columnEdges(x, spacing)[1]).toBe(columnEdges(x + spacing, spacing)[0]);
      }
    }
  });

  it("is at least one pixel wide when zoomed far out", () => {
    const [l, r] = columnEdges(100, 0.6);
    expect(r - l).toBeGreaterThanOrEqual(1);
  });
});

describe("background runs", () => {
  it("make one rectangle of bars side by side, and keep a gap a gap", () => {
    // spacing 10: bars at 100, 110, 120 touch; the one at 150 does not
    expect(backgroundRuns([100, 110, 120, 150], 10)).toEqual([
      [95, 125],
      [145, 155],
    ]);
  });

  it("is one column per bar when none touch, and nothing for no bars", () => {
    expect(backgroundRuns([10, 50], 4)).toEqual([
      [8, 12],
      [48, 52],
    ]);
    expect(backgroundRuns([], 10)).toEqual([]);
  });

  it("covers exactly what the separate columns would, at fractional spacing", () => {
    const xs = Array.from({ length: 40 }, (_, i) => 3.3 + i * 6.7);
    const merged = backgroundRuns(xs, 6.7);
    expect(merged).toHaveLength(1);
    expect(merged[0][0]).toBe(columnEdges(xs[0], 6.7)[0]);
    expect(merged[0][1]).toBe(columnEdges(xs[39], 6.7)[1]);
  });

  it("merges thousands of narrow columns into a few rectangles", () => {
    const xs = Array.from({ length: 2000 }, (_, i) => i * 0.6); // zoomed far out
    expect(backgroundRuns(xs, 0.6).length).toBeLessThanOrEqual(2);
  });
});
