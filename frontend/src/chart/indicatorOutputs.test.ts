import { describe, expect, it } from "vitest";

import type {
  AreaOutput,
  BackgroundOutput,
  FillOutput,
  HistogramOutput,
  IndicatorDef,
  IndicatorOutput,
  LevelOutput,
  LineOutput,
  MarkerOutput,
} from "../api";
import {
  FALLBACK_COLOR,
  backgroundTimes,
  buildBands,
  buildFill,
  buildMarkers,
  hitTimes,
  kindOf,
  levelValue,
  lineData,
  lineStyleCode,
  markerPlacement,
  outputWidth,
  paramColor,
  replaceWindow,
  seriesPlan,
  tailPoints,
  withOpacity,
} from "./indicatorOutputs";
import { newEntry } from "./indicators";

const def = (outputs: IndicatorOutput[], overlay = true): IndicatorDef => ({
  id: "x",
  name: "X",
  title: "X",
  overlay,
  builtin: false,
  params: [
    { name: "c1", type: "color", default: "#2962ff", label: "C1", group: "style" },
    { name: "c2", type: "color", default: "#ff6d00", label: "C2", group: "style" },
    { name: "w", type: "width", default: 2, label: "W", group: "style", min: 1, max: 4, step: 1 },
    { name: "lvl", type: "float", default: 70, label: "Level", group: "inputs" },
  ],
  outputs,
});

describe("what a kind is", () => {
  it("is a line when the catalog says nothing (M7 outputs have no kind)", () => {
    expect(kindOf({ name: "ema", label: "EMA", color: "color", width: "width" })).toBe("line");
    expect(kindOf({ kind: "histogram", name: "h", label: "H", color: "c1", base: 0 })).toBe("histogram");
  });
});

describe("colours come from the instance's params through the catalog's names", () => {
  const params = { c1: "#112233", c2: "#FF6D00", w: 3 };

  it("looks the parameter up by name", () => {
    expect(paramColor(params, "c1")).toBe("#112233");
    expect(paramColor(params, "c2")).toBe("#FF6D00");
  });

  it("follows a changed colour, not the default", () => {
    expect(paramColor({ ...params, c1: "#00ff00" }, "c1")).toBe("#00ff00");
  });

  it("falls back to grey for a name that is missing, not a colour, or null", () => {
    expect(paramColor(params, "nope")).toBe(FALLBACK_COLOR);
    expect(paramColor({ c1: "red" }, "c1")).toBe(FALLBACK_COLOR);
    expect(paramColor({ c1: 5 }, "c1")).toBe(FALLBACK_COLOR);
    expect(paramColor(params, null)).toBe(FALLBACK_COLOR);
    expect(paramColor(params, undefined)).toBe(FALLBACK_COLOR);
  });
});

describe("which outputs have a width to set in the Style tab", () => {
  it("lines, areas and levels name one (or null); the other kinds have none", () => {
    expect(outputWidth({ name: "l", label: "L", color: "c1", width: "w" })).toBe("w");
    expect(outputWidth({ kind: "area", name: "a", label: "A", color: "c1", width: null })).toBeNull();
    expect(outputWidth({ kind: "histogram", name: "h", label: "H", color: "c1", base: 0 })).toBeNull();
    expect(outputWidth({ kind: "fill", name: "fill:1", label: "F", between: ["a", "b"], color: "c1", opacity: 0.2 })).toBeNull();
    expect(outputWidth({ kind: "marker", name: "m", label: "M", color: "c1", shape: "circle", position: "above", text: "" })).toBeNull();
    expect(outputWidth({ kind: "background", name: "b", label: "B", color: "c1", opacity: 0.1 })).toBeNull();
  });
});

describe("opacity", () => {
  it("makes an rgba colour", () => {
    expect(withOpacity("#2962ff", 0.15)).toBe("rgba(41, 98, 255, 0.15)");
    expect(withOpacity("#FF6D00", 1)).toBe("rgba(255, 109, 0, 1)");
  });

  it("keeps an opacity of 1 and clamps anything outside 0 to 1", () => {
    expect(withOpacity("#000000", 1.5)).toBe("rgba(0, 0, 0, 1)");
    expect(withOpacity("#000000", -1)).toBe("rgba(0, 0, 0, 0)");
    expect(withOpacity("#000000", Number.NaN)).toBe("rgba(0, 0, 0, 0.15)");
  });
});

describe("line styles", () => {
  it("map to the library's codes (solid is the default and is not sent)", () => {
    expect(lineStyleCode("solid")).toBe(0);
    expect(lineStyleCode(undefined)).toBe(0);
    expect(lineStyleCode("dotted")).toBe(1);
    expect(lineStyleCode("dashed")).toBe(2);
  });
});

describe("hits are sparse indexes into t", () => {
  const t = [1000, 1060, 1120, 1180, 1240, 1300];

  it("become the times of those bars, in order, without expanding to the bar count", () => {
    expect(hitTimes([1, 4], t)).toEqual([1060, 1240]);
    expect(hitTimes([], t)).toEqual([]);
    expect(hitTimes(undefined, t)).toEqual([]);
  });

  it("count positions in t, so warmup bars the server never sent are not in the way", () => {
    // The server's t starts at the first bar asked for; index 0 is that bar.
    expect(hitTimes([0], t)).toEqual([1000]);
  });

  it("ignore positions that are outside t or not whole numbers", () => {
    expect(hitTimes([-1, 2, 6, 99, 1.5, Number.NaN], t)).toEqual([1120]);
  });

  it("do not allocate per bar: a thousand bars with a hit every tenth", () => {
    const bars = Array.from({ length: 1000 }, (_, i) => 1_000_000 + i * 60);
    const hits = Array.from({ length: 100 }, (_, i) => i * 10);
    const times = hitTimes(hits, bars);
    expect(times).toHaveLength(100);
    expect(times[1]).toBe(1_000_600);
  });
});

describe("levels", () => {
  const fixed: LevelOutput = { kind: "level", name: "level:1", label: "Upper", value: 70, input: null, color: "c1", width: null, style: "dashed" };
  const named: LevelOutput = { ...fixed, name: "level:2", value: null, input: "lvl" };

  it("take the number the server resolved", () => {
    expect(levelValue(fixed, { lvl: 1 }, { "level:1": 70 })).toBe(70);
    expect(levelValue(named, { lvl: 55 }, { "level:2": 55 })).toBe(55);
  });

  it("use the answer even when it differs from the entry's own input (the answer is for the request)", () => {
    expect(levelValue(named, { lvl: 80 }, { "level:2": 55 })).toBe(55);
  });

  it("fall back to the number or the input's value when the answer has none", () => {
    expect(levelValue(fixed, {}, undefined)).toBe(70);
    expect(levelValue(named, { lvl: 33 }, {})).toBe(33);
  });

  it("are undefined when there is nothing finite to draw at", () => {
    expect(levelValue(named, { lvl: "x" }, {})).toBeUndefined();
    expect(levelValue(named, {}, {})).toBeUndefined();
    expect(levelValue(fixed, {}, { "level:1": Number.NaN })).toBe(70);
  });
});

describe("markers", () => {
  const out = (over: Partial<MarkerOutput> = {}): MarkerOutput => ({
    kind: "marker",
    name: "m",
    label: "M",
    color: "c1",
    shape: "arrow_up",
    position: "below",
    text: "",
    ...over,
  });
  const toChart = (t: number) => t + 7200;
  const bar = { high: 12, low: 8, close: 11 };

  it("map the catalog's shapes and positions to the library's", () => {
    expect(markerPlacement(out({ shape: "arrow_up", position: "above" }), bar)).toEqual({ shape: "arrowUp", position: "atPriceTop", price: 12 });
    expect(markerPlacement(out({ shape: "arrow_down", position: "below" }), bar)).toEqual({ shape: "arrowDown", position: "atPriceBottom", price: 8 });
    expect(markerPlacement(out({ shape: "circle", position: "on" }), bar)).toEqual({ shape: "circle", position: "atPriceMiddle", price: 11 });
    expect(markerPlacement(out({ shape: "square", position: "above" }), bar).shape).toBe("square");
  });

  it("are placed at t[i] for each hit, in chart time, with the colour and text", () => {
    const t = [100, 160, 220, 280];
    const times = hitTimes([1, 3], t);
    const got = buildMarkers(out({ text: "Buy", position: "above" }), times, "#112233", () => bar, toChart);
    expect(got).toEqual([
      { time: 160 + 7200, position: "atPriceTop", price: 12, shape: "arrowUp", color: "#112233", text: "Buy" },
      { time: 280 + 7200, position: "atPriceTop", price: 12, shape: "arrowUp", color: "#112233", text: "Buy" },
    ]);
  });

  it("leave the text out when there is none", () => {
    const [m] = buildMarkers(out(), [160], "#112233", () => bar, toChart);
    expect("text" in m).toBe(false);
  });

  it("skip a bar the chart does not hold or has no price for", () => {
    const toChartHalf = (t: number) => (t === 160 ? undefined : t);
    const ref = (t: number) => (t === 220 ? undefined : bar);
    expect(buildMarkers(out(), [160, 220, 280], "#112233", ref, toChartHalf).map((m) => m.time)).toEqual([280]);
  });

  it("stay ascending in chart time", () => {
    const got = buildMarkers(out(), [100, 200, 300, 400], "#112233", () => bar, toChart);
    expect(got.map((m) => m.time)).toEqual([...got.map((m) => m.time)].sort((a, b) => a - b));
  });
});

describe("background columns", () => {
  it("are the chart times of the hit bars", () => {
    expect(backgroundTimes([100, 160, 220], (t) => t + 1)).toEqual([101, 161, 221]);
  });

  it("skip bars the chart does not hold", () => {
    expect(backgroundTimes([100, 160, 220], (t) => (t === 160 ? undefined : t))).toEqual([100, 220]);
  });
});

describe("fill between two outputs", () => {
  const m = (pairs: [number, number | null][]) => new Map(pairs);
  const identity = (t: number) => t;

  it("pairs the two outputs bar by bar, in order", () => {
    const f = buildFill(m([[1, 10], [2, 11], [3, 12]]), m([[1, 5], [2, 6], [3, 7]]), identity);
    expect(f.time).toEqual([1, 2, 3]);
    expect(Array.from(f.upper)).toEqual([10, 11, 12]);
    expect(Array.from(f.lower)).toEqual([5, 6, 7]);
  });

  it("marks a bar where either side has no value as a gap (NaN on both), so the band is cut there", () => {
    const f = buildFill(m([[1, 10], [2, null], [3, 12], [4, 13]]), m([[1, 5], [2, 6], [3, null], [4, 8]]), identity);
    expect(f.time).toEqual([1, 2, 3, 4]);
    expect(f.upper[0]).toBe(10);
    expect(Number.isNaN(f.upper[1]) && Number.isNaN(f.lower[1])).toBe(true);
    expect(Number.isNaN(f.upper[2]) && Number.isNaN(f.lower[2])).toBe(true);
    expect(f.lower[3]).toBe(8);
  });

  it("treats a bar missing from the second output as a gap", () => {
    const f = buildFill(m([[1, 10], [2, 11]]), m([[1, 5]]), identity);
    expect(Number.isNaN(f.upper[1])).toBe(true);
  });

  it("converts to chart time and skips bars the chart does not hold", () => {
    const f = buildFill(m([[1, 10], [2, 11]]), m([[1, 5], [2, 6]]), (t) => (t === 1 ? undefined : t + 100));
    expect(f.time).toEqual([102]);
  });

  it("is empty for empty inputs", () => {
    expect(buildFill(m([]), m([]), identity).time).toEqual([]);
  });
});

describe("the data of fills and backgrounds", () => {
  const fill: FillOutput = { kind: "fill", name: "fill:1", label: "F", between: ["a", "b"], color: "c1", opacity: 0.2 };
  const bg: BackgroundOutput = { kind: "background", name: "bg", label: "B", color: "c2", opacity: 0.1 };
  const a: LineOutput = { name: "a", label: "A", color: "c1", width: "w" };
  const b: LineOutput = { name: "b", label: "B", color: "c2", width: "w" };
  const d = def([a, b, fill, bg]);
  const values = new Map([
    ["a", new Map<number, number | null>([[1, 10], [2, 11]])],
    ["b", new Map<number, number | null>([[1, 5], [2, 6]])],
  ]);

  it("is built from the values and hits held, for every fill and background, by output name", () => {
    const got = buildBands(d, values, new Map([["bg", [1, 2]]]), (t) => t + 100);
    expect(got.fills.get("fill:1")?.time).toEqual([101, 102]);
    expect(Array.from(got.fills.get("fill:1")!.upper)).toEqual([10, 11]);
    expect(got.backgrounds.get("bg")).toEqual([101, 102]);
  });

  it("has an empty entry, not none, when there is nothing yet (a hidden indicator is shown again later)", () => {
    const got = buildBands(d, new Map([["a", new Map()], ["b", new Map()]]), new Map(), (t) => t);
    expect(got.fills.get("fill:1")?.time).toEqual([]);
    expect(got.backgrounds.get("bg")).toEqual([]);
  });

  it("has no fill for one whose outputs are not value outputs of the definition", () => {
    const broken = def([a, { ...fill, between: ["a", "nope"] as [string, string] }]);
    expect(buildBands(broken, values, new Map(), (t) => t).fills.size).toBe(0);
  });
});

describe("replacing a window of times (the forming bar's update)", () => {
  it("swaps what was in the window for the new times and leaves the rest", () => {
    expect(replaceWindow([1, 3, 5, 7, 9], 5, 9, [7])).toEqual([1, 3, 7]);
    expect(replaceWindow([1, 3, 5], 5, 9, [5, 7])).toEqual([1, 3, 5, 7]);
    expect(replaceWindow([1, 3, 5], 5, 9, [])).toEqual([1, 3]);
    expect(replaceWindow([], 0, 9, [4])).toEqual([4]);
  });

  it("keeps the order", () => {
    const got = replaceWindow([2, 4, 6, 8], 6, 100, [7, 9]);
    expect(got).toEqual([2, 4, 7, 9]);
  });
});

describe("value series", () => {
  const toChart = (t: number) => t + 10;

  it("lineData keeps order, skips gaps and bars the chart lacks", () => {
    const values = new Map<number, number | null>([[1, 5], [2, null], [3, 7], [4, 8]]);
    expect(lineData(values, (t) => (t === 4 ? undefined : toChart(t)))).toEqual([
      { time: 11, value: 5 },
      { time: 13, value: 7 },
    ]);
  });

  it("a histogram plans columns from its base", () => {
    const o: HistogramOutput = { kind: "histogram", name: "h", label: "H", color: "c1", base: 2.5 };
    const entry = newEntry(def([o]));
    const [p] = seriesPlan(def([o]), entry);
    expect(p).toMatchObject({ name: "h", kind: "histogram" });
    expect(p.options).toMatchObject({ color: "#2962ff", base: 2.5, visible: true, lastValueVisible: true });
  });

  it("an area plans a line with a shaded area below it in the same colour", () => {
    const o: AreaOutput = { kind: "area", name: "a", label: "A", color: "c2", width: "w" };
    const [p] = seriesPlan(def([o]), newEntry(def([o])));
    expect(p.kind).toBe("area");
    expect(p.options).toMatchObject({ lineColor: "#ff6d00", lineWidth: 2 });
    expect(String(p.options.topColor)).toMatch(/^rgba\(255, 109, 0, 0\.\d+\)$/);
    expect(String(p.options.bottomColor)).toMatch(/^rgba\(255, 109, 0, 0(\.0\d*)?\)$/);
  });

  it("an area without a width param still has a line", () => {
    const o: AreaOutput = { kind: "area", name: "a", label: "A", color: "c2", width: null };
    const [p] = seriesPlan(def([o]), newEntry(def([o])));
    expect(Number(p.options.lineWidth)).toBeGreaterThanOrEqual(1);
  });

  it("a dashed or dotted line says so, and takes its colour and width from the params", () => {
    const o: LineOutput = { kind: "line", name: "l", label: "L", color: "c2", width: "w", style: "dashed" };
    const d = def([o]);
    const entry = { ...newEntry(d), params: { ...newEntry(d).params, c2: "#abcdef", w: 4 } };
    const [p] = seriesPlan(d, entry);
    expect(p.options).toMatchObject({ color: "#abcdef", lineWidth: 4, lineStyle: 2 });
    const [dotted] = seriesPlan(def([{ ...o, style: "dotted" }]), entry);
    expect(dotted.options.lineStyle).toBe(1);
  });

  it("hiding and the price label switch follow the entry, for every kind", () => {
    const outs: IndicatorOutput[] = [
      { kind: "line", name: "l", label: "L", color: "c1", width: "w", style: "solid" },
      { kind: "histogram", name: "h", label: "H", color: "c1", base: 0 },
      { kind: "area", name: "a", label: "A", color: "c1", width: null },
    ];
    const d = def(outs);
    const entry = { ...newEntry(d), visible: false, price_labels: false };
    for (const p of seriesPlan(d, entry)) expect(p.options).toMatchObject({ visible: false, lastValueVisible: false });
  });

  it("plans only the outputs that have values: fill, level, marker and background are not series", () => {
    const fill: FillOutput = { kind: "fill", name: "fill:1", label: "F", between: ["l", "l2"], color: "c1", opacity: 0.2 };
    const level: LevelOutput = { kind: "level", name: "level:1", label: "L", value: 30, input: null, color: "c1", width: null, style: "dotted" };
    const marker: MarkerOutput = { kind: "marker", name: "m", label: "M", color: "c1", shape: "circle", position: "above", text: "" };
    const bg: BackgroundOutput = { kind: "background", name: "b", label: "B", color: "c1", opacity: 0.1 };
    const line: LineOutput = { kind: "line", name: "l", label: "L", color: "c1", width: "w", style: "solid" };
    const d = def([line, fill, level, marker, bg]);
    expect(seriesPlan(d, newEntry(d)).map((p) => p.name)).toEqual(["l"]);
  });
});

describe("the forming bar's update", () => {
  it("applies the values the answer has, for bars the chart holds, in order", () => {
    expect(tailPoints([100, 160], [1.5, 1.6], (t) => t + 7)).toEqual([
      { t: 100, time: 107, value: 1.5 },
      { t: 160, time: 167, value: 1.6 },
    ]);
  });

  it("never lets a missing value wipe one already held (a file with no warmup answers null for a short window)", () => {
    expect(tailPoints([100, 160], [null, 1.6], (t) => t)).toEqual([{ t: 160, time: 160, value: 1.6 }]);
    expect(tailPoints([100, 160], [null, null], (t) => t)).toEqual([]);
  });

  it("skips a bar the chart does not hold", () => {
    expect(tailPoints([100, 160], [1, 2], (t) => (t === 100 ? undefined : t))).toEqual([{ t: 160, time: 160, value: 2 }]);
  });
});

describe("compatibility: the built-in EMA and an M7-format file draw as before", () => {
  // The EMA as GET /api/indicators described it before CI-2 (b): no kind, no style.
  const EMA: IndicatorDef = {
    id: "ema",
    name: "EMA",
    title: "Moving Average Exponential",
    overlay: true,
    builtin: true,
    params: [
      { name: "length", type: "int", default: 9, label: "Length", group: "inputs", min: 1, max: 5000, step: 1 },
      { name: "source", type: "source", default: "close", label: "Source", group: "inputs", choices: ["close"] },
      { name: "color", type: "color", default: "#2962ff", label: "Colour", group: "style" },
      { name: "width", type: "width", default: 1, label: "Width", group: "style", min: 1, max: 4, step: 1 },
    ],
    outputs: [{ name: "ema", label: "EMA", color: "color", width: "width" } as LineOutput],
  };

  it("creates the same series with the same options as the M7 chart did", () => {
    const entry = newEntry(EMA);
    const plan = seriesPlan(EMA, entry);
    expect(plan).toHaveLength(1);
    expect(plan[0].kind).toBe("line");
    // The M7 chart: addSeries(LineSeries, { priceLineVisible: false, crosshairMarkerRadius: 3 }) ...
    expect(plan[0].create).toEqual({ priceLineVisible: false, crosshairMarkerRadius: 3 });
    // ... then applyOptions with exactly these (a solid line sends no lineStyle).
    expect(plan[0].options).toEqual({ color: "#2962ff", lineWidth: 1, visible: true, lastValueVisible: true, title: "" });
  });

  it("follows a changed colour, width, hidden state and price label as before", () => {
    const entry = { ...newEntry(EMA), visible: false, price_labels: false, params: { ...newEntry(EMA).params, color: "#ff0000", width: 3 } };
    expect(seriesPlan(EMA, entry)[0].options).toEqual({ color: "#ff0000", lineWidth: 3, visible: false, lastValueVisible: false, title: "" });
  });

  it("an M7 file with several lines plans one line each, in declared order, and nothing else", () => {
    const m7: IndicatorDef = {
      ...EMA,
      id: "my_two",
      builtin: false,
      params: [...EMA.params, { name: "color2", type: "color", default: "#ff6d00", label: "Colour 2", group: "style" }],
      outputs: [
        { name: "fast", label: "Fast", color: "color", width: "width" } as LineOutput,
        { name: "slow", label: "Slow", color: "color2", width: "width" } as LineOutput,
      ],
    };
    const plan = seriesPlan(m7, newEntry(m7));
    expect(plan.map((p) => [p.name, p.kind, p.options.color])).toEqual([
      ["fast", "line", "#2962ff"],
      ["slow", "line", "#ff6d00"],
    ]);
  });

  it("sends the values to the series as the M7 chart did: ordered, no gaps", () => {
    const values = new Map<number, number | null>([[60, null], [120, 1.5], [180, 1.6]]);
    expect(lineData(values, (t) => t)).toEqual([
      { time: 120, value: 1.5 },
      { time: 180, value: 1.6 },
    ]);
  });
});
