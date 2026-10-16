import { describe, expect, it } from "vitest";
import type { Bar } from "../api";
import { mergeBars } from "./merge";

const bar = (t: number, c = 1, v = 1): Bar => ({ t, o: 1, h: 2, l: 0.5, c, v });

describe("mergeBars", () => {
  it("appends newer bars as updates", () => {
    const r = mergeBars([bar(60), bar(120)], [bar(120, 1.5), bar(180)]);
    expect(r.rewritesHistory).toBe(false);
    expect(r.tail.map((b) => b.t)).toEqual([120, 180]);
    expect(r.bars.map((b) => b.t)).toEqual([60, 120, 180]);
    expect(r.bars[1].c).toBe(1.5);
  });

  it("inserts a bar missed during a disconnect", () => {
    // We had 02:19 and the live 02:21; the server later stored the official 02:20.
    const r = mergeBars([bar(60), bar(180)], [bar(60), bar(120), bar(180)]);
    expect(r.rewritesHistory).toBe(true);
    expect(r.bars.map((b) => b.t)).toEqual([60, 120, 180]);
  });

  it("replaces a corrected older bar", () => {
    const r = mergeBars([bar(60, 1), bar(120)], [bar(60, 1.1)]);
    expect(r.rewritesHistory).toBe(true);
    expect(r.bars[0].c).toBe(1.1);
  });

  it("ignores identical older bars", () => {
    const r = mergeBars([bar(60), bar(120)], [bar(60), bar(120)]);
    expect(r.rewritesHistory).toBe(false);
  });

  it("handles an empty chart", () => {
    expect(mergeBars([], [bar(60)]).bars).toHaveLength(1);
  });

  it("does not regress the live bar with the server's partial copy", () => {
    const live = { ...bar(120, 2.5, 30), complete: false };
    const r = mergeBars([bar(60), live], [bar(60), { ...bar(120, 2.0, 20), complete: false }]);
    expect(r.tail).toEqual([]);
    expect(r.bars[1].c).toBe(2.5);
  });

  it("completes our last bar when the server has the finished version", () => {
    const stale = { ...bar(120, 2.5, 30), complete: false };
    const r = mergeBars([bar(60), stale], [{ ...bar(120, 2.7, 44), complete: true }, bar(180)]);
    expect(r.tail.map((b) => [b.t, b.c])).toEqual([
      [120, 2.7],
      [180, 1],
    ]);
  });

  describe("with the server's forming bar", () => {
    // The answer now ends with the bar that holds the live minute (`forming`, not complete).
    const forming = (t: number, c: number, v: number): Bar => ({ ...bar(t, c, v), complete: false, forming: true });

    it("never replaces our live copy of the same bar, even if its volume is larger", () => {
      const live = { ...bar(120, 2.5, 30), complete: false };
      const r = mergeBars([bar(60), live], [bar(60), forming(120, 2.0, 40)]);
      expect(r.tail).toEqual([]);
      expect(r.bars[1].c).toBe(2.5);
    });

    it("is applied when it is a bar that started while we were away", () => {
      const r = mergeBars([bar(60), bar(120)], [bar(120), forming(180, 3, 5)]);
      expect(r.rewritesHistory).toBe(false);
      expect(r.tail.map((b) => [b.t, b.forming])).toEqual([
        [120, undefined],
        [180, true],
      ]);
      expect(r.bars.map((b) => b.t)).toEqual([60, 120, 180]);
    });

    it("is replaced by the finished bar later, which carries no flag", () => {
      const r = mergeBars([bar(60), forming(120, 2.5, 30)], [{ ...bar(120, 2.7, 44), complete: true }]);
      expect(r.tail.map((b) => [b.t, b.c, b.forming])).toEqual([[120, 2.7, undefined]]);
      expect(r.bars[1].forming).toBeUndefined();
    });
  });
});
