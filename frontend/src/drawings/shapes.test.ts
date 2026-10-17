import { describe, expect, it } from "vitest";
import { TimeIndex } from "./geometry";
import {
  FIB_LEVELS,
  channelLines,
  contrastColor,
  fillOf,
  isLineType,
  styleOptions,
  channelOffset,
  dragHandle,
  fibLevels,
  formatDuration,
  measureStats,
  moveShape,
  rectBox,
  rectCorners,
  shapeError,
  textSize,
  withAlpha,
} from "./shapes";
import type { Clock } from "./tradingTime";
import fixture from "./shape-rules.fixture.json";

const identity: Clock = (t) => t;
/** Time inside [lo, hi] does not count (a weekend, a holiday). */
const closure =
  (lo: number, hi: number): Clock =>
  (t) =>
    t - Math.max(0, Math.min(t, hi) - lo);
const P = (t: number, p: number) => ({ t, p });

describe("shapeError (shared fixture with the server)", () => {
  for (const c of fixture.cases as { name: string; type: string; points: number[][]; ok: boolean; closure?: number[]; label?: string | null }[]) {
    it(c.name, () => {
      const clock = c.closure ? closure(c.closure[0], c.closure[1]) : identity;
      const err = shapeError(c.type as never, c.points.map(([t, p]) => P(t, p)), clock, c.label);
      expect(err === null).toBe(c.ok);
    });
  }
});

describe("channel", () => {
  const base = [P(0, 0), P(100, 10)];

  it("measures the offset of the third point from the extended base line", () => {
    expect(channelOffset([...base, P(50, 8)], identity)).toBeCloseTo(3);
    expect(channelOffset([...base, P(300, 25)], identity)).toBeCloseTo(-5);
  });

  it("measures the offset in trading time, as the server does", () => {
    // Time between 100 and 200 does not count: the same case as tests/test_drawing_tools.py.
    const pts = [P(0, 0), P(300, 15), P(250, 13.25)];
    expect(channelOffset(pts, closure(100, 200))).toBeCloseTo(2);
    expect(channelOffset(pts, identity)).toBeCloseTo(0.75);
  });

  it("draws the base and a parallel line moved by the offset", () => {
    const [a, b] = channelLines([...base, P(50, 8)], identity, false);
    expect(a).toEqual({ type: "trendline", pts: base });
    expect(b).toEqual({ type: "trendline", pts: [P(0, 3), P(100, 13)] });
  });

  it("becomes two rays with extend", () => {
    const lines = channelLines([...base, P(50, 8)], identity, true);
    expect(lines.map((l) => l.type)).toEqual(["ray", "ray"]);
  });

  it("stays the same distance apart in price across a closure (trading time)", () => {
    const clock = closure(100, 200);
    const pts = [P(0, 0), P(300, 15), P(250, 13.25)];
    const d = channelOffset(pts, clock);
    const [lo, hi] = channelLines(pts, clock, false);
    for (const t of [0, 50, 100, 150, 200, 250, 300]) {
      const dy = (q: { pts: { t: number; p: number }[] }) => q.pts[0].p + ((q.pts[1].p - q.pts[0].p) * (clock(t) - clock(q.pts[0].t))) / (clock(q.pts[1].t) - clock(q.pts[0].t));
      expect(dy(hi) - dy(lo)).toBeCloseTo(d);
    }
  });
});

describe("fibLevels", () => {
  it("puts level 0 at the end and level 1 at the start", () => {
    const levels = fibLevels([P(0, 100), P(60, 200)]);
    expect(levels.map((l) => l.level)).toEqual(FIB_LEVELS);
    expect(levels[0].price).toBe(200);
    expect(levels[levels.length - 1].price).toBe(100);
    expect(levels.find((l) => l.level === 0.5)!.price).toBeCloseTo(150);
    expect(levels.find((l) => l.level === 0.618)!.price).toBeCloseTo(138.2);
  });

  it("works downwards", () => {
    const levels = fibLevels([P(0, 200), P(60, 100)]);
    expect(levels.find((l) => l.level === 0.382)!.price).toBeCloseTo(138.2);
  });
});

describe("rect", () => {
  it("normalises the corners whatever the order", () => {
    expect(rectBox([P(200, 9), P(100, 5)])).toEqual({ t0: 100, t1: 200, p0: 5, p1: 9 });
  });

  it("has four corner handles", () => {
    expect(rectCorners([P(100, 5), P(200, 9)])).toEqual([P(100, 5), P(200, 5), P(200, 9), P(100, 9)]);
  });
});

describe("dragHandle", () => {
  it("moves a corner: its time from one anchor, its price from the other", () => {
    const r = [P(100, 5), P(200, 9)];
    expect(dragHandle("rect", r, 0, P(90, 4), identity)).toEqual([P(90, 4), P(200, 9)]);
    expect(dragHandle("rect", r, 1, P(250, 4), identity)).toEqual([P(100, 4), P(250, 9)]);
    expect(dragHandle("rect", r, 2, P(260, 12), identity)).toEqual([P(100, 5), P(260, 12)]);
    expect(dragHandle("rect", r, 3, P(50, 10), identity)).toEqual([P(50, 5), P(200, 10)]);
  });

  it("refuses a move that breaks a rule", () => {
    expect(dragHandle("rect", [P(100, 5), P(200, 9)], 1, P(250, 9), identity)).toBeNull(); // flat
    expect(dragHandle("fib", [P(100, 5), P(200, 9)], 1, P(100, 12), identity)).toBeNull(); // same time
    expect(dragHandle("trendline", [P(100, 5), P(200, 9)], 0, P(200, 1), identity)).toBeNull();
  });

  it("moves one end of a line, measure or fib", () => {
    expect(dragHandle("measure", [P(100, 5), P(200, 9)], 1, P(300, 1), identity)).toEqual([P(100, 5), P(300, 1)]);
    expect(dragHandle("trendline", [P(100, 5), P(200, 9)], 0, P(50, 2), identity)).toEqual([P(50, 2), P(200, 9)]);
  });

  it("moves a vertical line in time only, a horizontal one in price only", () => {
    expect(dragHandle("vline", [P(100, 5)], 0, P(300, 99), identity)).toEqual([P(300, 5)]);
    expect(dragHandle("hline", [P(100, 5)], 0, P(300, 99), identity)).toEqual([P(100, 99)]);
    expect(dragHandle("text", [P(100, 5)], 0, P(300, 99), identity)).toEqual([P(300, 99)]);
  });

  it("keeps the channel's offset when a base end moves, and sets it when the third point moves", () => {
    const c = [P(0, 0), P(100, 10), P(50, 8)]; // offset 3
    const moved = dragHandle("channel", c, 0, P(0, 2), identity)!;
    expect(moved.slice(0, 2)).toEqual([P(0, 2), P(100, 10)]);
    expect(channelOffset(moved, identity)).toBeCloseTo(3);
    const third = dragHandle("channel", c, 2, P(70, 12), identity)!;
    expect(third[2]).toEqual(P(70, 12));
    expect(channelOffset(third, identity)).toBeCloseTo(5);
    expect(dragHandle("channel", c, 2, P(50, 5), identity)).toBeNull(); // onto the base line
    expect(dragHandle("channel", c, 1, P(0, 10), identity)).toBeNull(); // no time between the base ends
  });
});

describe("moveShape", () => {
  const index = new TimeIndex([0, 60, 120, 180, 240, 300], 60);
  const round = (p: number) => Math.round(p * 100) / 100;

  it("shifts every anchor by whole bars and a price", () => {
    expect(moveShape("rect", [P(60, 5), P(120, 9)], 2, 1.234, index, round)).toEqual([P(180, 6.23), P(240, 10.23)]);
  });

  it("moves a vertical line in time only and a horizontal one in price only", () => {
    expect(moveShape("vline", [P(60, 5)], 2, 3, index, round)).toEqual([P(180, 5)]);
    expect(moveShape("hline", [P(60, 5)], 2, 3, index, round)).toEqual([P(60, 8)]);
  });

  it("moves all three anchors of a channel together", () => {
    const moved = moveShape("channel", [P(0, 0), P(120, 10), P(60, 8)], 1, 2, index, round);
    expect(moved).toEqual([P(60, 2), P(180, 12), P(120, 10)]);
  });
});

describe("measureStats", () => {
  const index = new TimeIndex([0, 60, 120, 180, 240, 300], 60);

  it("gives the change, percent, bars, time and pips", () => {
    const s = measureStats([P(60, 100), P(240, 110)], index, 0.1);
    expect(s.change).toBeCloseTo(10);
    expect(s.percent).toBeCloseTo(10);
    expect(s.bars).toBe(3);
    expect(s.seconds).toBe(180);
    expect(s.pips).toBeCloseTo(100);
  });

  it("is negative the other way", () => {
    const s = measureStats([P(240, 110), P(60, 100)], index, 0.1);
    expect(s.change).toBeCloseTo(-10);
    expect(s.percent).toBeCloseTo(-9.0909, 3);
    expect(s.bars).toBe(-3);
    expect(s.seconds).toBe(-180);
  });
});

describe("formatting", () => {
  it("formats a duration with two units at most", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(59)).toBe("59s");
    expect(formatDuration(60)).toBe("1m");
    expect(formatDuration(5400)).toBe("1h 30m");
    expect(formatDuration(2 * 86400 + 3 * 3600 + 120)).toBe("2d 3h");
    expect(formatDuration(-5400)).toBe("1h 30m");
  });

  it("makes a translucent colour from a hex colour and a percent", () => {
    expect(withAlpha("#4d8dff", 15)).toBe("rgba(77,141,255,0.15)");
    expect(withAlpha("#000000", 0)).toBe("rgba(0,0,0,0)");
    expect(withAlpha("#FFFFFF", 100)).toBe("rgba(255,255,255,1)");
    expect(withAlpha("#ffffff", 150)).toBe("rgba(255,255,255,1)");
    expect(withAlpha("#ffffff", -20)).toBe("rgba(255,255,255,0)");
  });

  it("maps the width 1-4 to a text size", () => {
    expect([1, 2, 3, 4].map(textSize)).toEqual([12, 14, 18, 24]);
    expect(textSize(0)).toBe(12);
    expect(textSize(9)).toBe(24);
  });
});

describe("per-type options", () => {
  it("knows which tools an alert can follow", () => {
    expect((["hline", "trendline", "ray", "extended"] as const).every(isLineType)).toBe(true);
    expect((["vline", "rect", "channel", "fib", "text", "measure"] as const).some(isLineType)).toBe(false);
  });

  it("offers each tool only the style controls that apply", () => {
    expect(styleOptions("rect")).toEqual({ dash: true, fill: true, extend: false, label: true });
    expect(styleOptions("channel")).toEqual({ dash: true, fill: true, extend: true, label: true });
    expect(styleOptions("fib")).toEqual({ dash: true, fill: false, extend: true, label: false });
    expect(styleOptions("text")).toEqual({ dash: false, fill: false, extend: false, label: true });
    expect(styleOptions("measure")).toEqual({ dash: true, fill: false, extend: false, label: false });
    expect(styleOptions("vline")).toEqual({ dash: true, fill: false, extend: false, label: true });
  });

  it("uses the stored fill, else the tool's default", () => {
    expect(fillOf("rect", undefined)).toBe(15);
    expect(fillOf("channel", undefined)).toBe(10);
    expect(fillOf("fib", undefined)).toBe(0);
    expect(fillOf("rect", 0)).toBe(0);
    expect(fillOf("rect", 40)).toBe(40);
  });

  it("picks a readable text colour", () => {
    expect(contrastColor("#ffffff")).toBe("#111111");
    expect(contrastColor("#000000")).toBe("#ffffff");
    expect(contrastColor("#4d8dff")).toBe("#ffffff");
    expect(contrastColor("#eab308")).toBe("#111111");
  });
});
