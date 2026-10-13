import { describe, expect, it } from "vitest";
import type { Bar } from "../api";
import { haStep, heikinAshi } from "./heikinAshi";

const bar = (t: number, o: number, h: number, l: number, c: number): Bar => ({ t, o, h, l, c, v: 1 });

describe("Heikin Ashi (display only)", () => {
  it("follows the standard formulas, seeded from the first real bar", () => {
    const real = [bar(0, 10, 14, 9, 12), bar(60, 12, 13, 8, 9), bar(120, 9, 11, 9, 11)];
    const ha = heikinAshi(real);
    expect(ha[0]).toMatchObject({ t: 0, o: 11, c: 11.25, h: 14, l: 9 });
    // open = (11 + 11.25) / 2, close = (12 + 13 + 8 + 9) / 4
    expect(ha[1]).toMatchObject({ t: 60, o: 11.125, c: 10.5, h: 13, l: 8 });
    // open = (11.125 + 10.5) / 2, close = 40 / 4
    expect(ha[2]).toMatchObject({ o: 10.8125, c: 10, h: 11, l: 9 });
    expect(real[1]).toEqual(bar(60, 12, 13, 8, 9)); // the real bars are not touched
  });

  it("keeps the high and low around the Heikin Ashi open and close", () => {
    const [a, b] = heikinAshi([bar(0, 100, 101, 99, 100), bar(60, 90, 91, 89, 90)]);
    expect(a.o).toBe(100);
    expect(b.o).toBe(100); // far above the real bar
    expect(b.h).toBe(100);
    expect(b.l).toBe(89);
  });

  it("recomputes the forming bar from the real one, with a fixed open", () => {
    const closed = heikinAshi([bar(0, 10, 12, 9, 11)]);
    const first = haStep(closed[0], { ...bar(60, 11, 11, 11, 11), complete: false });
    const later = haStep(closed[0], { ...bar(60, 11, 15, 10, 14), complete: false });
    expect(first.o).toBe(later.o);
    expect(later.c).toBe((11 + 15 + 10 + 14) / 4);
    expect(later.h).toBe(15);
    expect(later.complete).toBe(false);
    // Same result as recomputing the whole series with that bar.
    expect(heikinAshi([bar(0, 10, 12, 9, 11), bar(60, 11, 15, 10, 14)])[1]).toMatchObject({ o: later.o, c: later.c });
  });
});
