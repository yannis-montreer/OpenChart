/**
 * From the catalog's outputs and the values answer to what the chart draws (CI-2 (d)).
 * Pure: no chart, no DOM. `indicators.ts` creates the series and primitives; this module
 * decides their options and turns `t`, `values`, `hits` and `levels` into their data.
 *
 * Hits are sparse lists of positions in `t` and stay that way: a bar that is not a hit costs
 * nothing, here or in the chart.
 */

import type {
  AreaOutput,
  HistogramOutput,
  IndicatorDef,
  IndicatorEntry,
  IndicatorOutput,
  IndicatorParams,
  LevelOutput,
  LineOutput,
  LineStyleName,
  MarkerOutput,
} from "../api";

export type OutputKind = IndicatorOutput["kind"] & string;
export type ValueOutput = LineOutput | HistogramOutput | AreaOutput;

/** Used where an output names a colour the params do not hold (a bad or missing name). */
export const FALLBACK_COLOR = "#787b86";
const FALLBACK_OPACITY = 0.15;

/** M7 outputs have no `kind`: they are solid lines. */
export function kindOf(o: IndicatorOutput): OutputKind {
  return o.kind ?? "line";
}

export function isValueOutput(o: IndicatorOutput): o is ValueOutput {
  const k = kindOf(o);
  return k === "line" || k === "histogram" || k === "area";
}

export function valueOutputs(def: IndicatorDef): ValueOutput[] {
  return def.outputs.filter(isValueOutput);
}

/** The parameter holding an output's width, for the kinds that have one. */
export function outputWidth(o: IndicatorOutput): string | null {
  return "width" in o ? o.width : null;
}

/** The colour the instance holds under the parameter `name`; grey if there is none. */
export function paramColor(params: IndicatorParams, name: string | null | undefined): string {
  const v = name ? params[name] : undefined;
  return typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v) ? v : FALLBACK_COLOR;
}

/** A line width from the parameter `name` (1 to 4), or `fallback`. */
export function paramWidth(params: IndicatorParams, name: string | null | undefined, fallback: number): 1 | 2 | 3 | 4 {
  const v = name ? params[name] : undefined;
  const w = typeof v === "number" && Number.isFinite(v) ? Math.round(v) : fallback;
  return Math.min(4, Math.max(1, w)) as 1 | 2 | 3 | 4;
}

/** `#rrggbb` and an opacity as a CSS colour. An opacity outside 0 to 1 is clamped. */
export function withOpacity(color: string, opacity: number): string {
  const a = Number.isFinite(opacity) ? Math.min(1, Math.max(0, opacity)) : FALLBACK_OPACITY;
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color);
  if (!m) return color;
  const [r, g, b] = m.slice(1).map((x) => parseInt(x, 16));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

/** Lightweight Charts' `LineStyle` numbers (solid is its default). */
export function lineStyleCode(style: LineStyleName | undefined): 0 | 1 | 2 {
  return style === "dashed" ? 2 : style === "dotted" ? 1 : 0;
}

// Value series: line, histogram, area

export interface SeriesPlan {
  name: string;
  kind: "line" | "histogram" | "area";
  /** Options when the series is created. */
  create: Record<string, unknown>;
  /** Options applied after creation and again whenever the entry changes. */
  options: Record<string, unknown>;
}

/** One plan per output that has values, in declared order. */
export function seriesPlan(def: IndicatorDef, entry: IndicatorEntry): SeriesPlan[] {
  const p = entry.params;
  const common = { visible: entry.visible, lastValueVisible: entry.price_labels, title: "" };
  return valueOutputs(def).map((o): SeriesPlan => {
    switch (kindOf(o)) {
      case "histogram": {
        const h = o as HistogramOutput;
        return { name: o.name, kind: "histogram", create: { priceLineVisible: false }, options: { color: paramColor(p, h.color), base: h.base, ...common } };
      }
      case "area": {
        const a = o as AreaOutput;
        const color = paramColor(p, a.color);
        return {
          name: o.name,
          kind: "area",
          create: { priceLineVisible: false, crosshairMarkerRadius: 3 },
          options: {
            lineColor: color,
            lineWidth: paramWidth(p, a.width, 2),
            topColor: withOpacity(color, 0.28),
            bottomColor: withOpacity(color, 0.02),
            ...common,
          },
        };
      }
      default: {
        const l = o as LineOutput;
        const style = lineStyleCode(l.style);
        return {
          name: o.name,
          kind: "line",
          create: { priceLineVisible: false, crosshairMarkerRadius: 3 },
          options: {
            color: paramColor(p, l.color),
            lineWidth: paramWidth(p, l.width, 1),
            ...(style ? { lineStyle: style } : {}),
            ...common,
          },
        };
      }
    }
  });
}

/** Colour of a point that hides the segment joining it to the one before. */
export const GAP_COLOR = "rgba(0, 0, 0, 0)";

export type SeriesPoint = {
  time: number;
  value: number;
  color?: string;
  lineColor?: string;
  topColor?: string;
  bottomColor?: string;
};

/**
 * Series data for one output: ascending, only bars the chart holds, no points for nulls. The
 * library joins a line across missing points (a point without a value does not break it), so a
 * gap between two values is drawn by giving the first value after it a transparent colour: the
 * segment that leads to a point takes that point's colour. A line breaks like that, and so does an
 * area; columns have nothing to join. Nothing is marked before the first value (warmup), after the
 * last, or on the last point (a transparent colour would hide its price label), so a series
 * whose only nulls are the warmup is exactly what the M7 chart drew.
 */
export function lineData(
  values: ReadonlyMap<number, number | null>,
  toChart: (t: number) => number | undefined,
  kind: "line" | "area" | "histogram" = "line",
): SeriesPoint[] {
  const data: SeriesPoint[] = [];
  const afterGap: number[] = []; // indexes in `data` of the first value after a gap
  let gap = false;
  for (const [t, value] of values) {
    const time = toChart(t);
    if (time === undefined) continue;
    if (value === null) {
      gap = data.length > 0;
      continue;
    }
    if (gap) afterGap.push(data.length);
    gap = false;
    data.push({ time, value });
  }
  if (kind !== "histogram") {
    for (const i of afterGap) {
      if (i === data.length - 1) continue;
      Object.assign(data[i], kind === "area" ? { lineColor: GAP_COLOR, topColor: GAP_COLOR, bottomColor: GAP_COLOR } : { color: GAP_COLOR });
    }
  }
  return data;
}

export interface TailPoint {
  t: number;
  time: number;
  value: number;
}

/**
 * What an answer for the last bars changes: a value for each bar the chart holds. A missing
 * value changes nothing, as in the M7 chart: a file that declares no warmup answers null for
 * a window this short, and must not blank what the full load showed.
 */
export function tailPoints(t: readonly number[], values: readonly (number | null)[], toChart: (t: number) => number | undefined): TailPoint[] {
  const out: TailPoint[] = [];
  t.forEach((bar, i) => {
    const value = values[i] ?? null;
    const time = toChart(bar);
    if (value !== null && time !== undefined) out.push({ t: bar, time, value });
  });
  return out;
}

// Hits: markers and backgrounds

/** Start times of the bars an output is on. `hits` are positions in `t`; ones outside it are ignored. */
export function hitTimes(hits: readonly number[] | undefined, t: readonly number[]): number[] {
  const out: number[] = [];
  if (!hits) return out;
  for (const i of hits) if (Number.isInteger(i) && i >= 0 && i < t.length) out.push(t[i]);
  return out;
}

/** `times` without what lay in `[from, to]`, then `add` (ascending, inside the window) put in its place. */
export function replaceWindow(times: readonly number[], from: number, to: number, add: readonly number[]): number[] {
  let lo = 0;
  while (lo < times.length && times[lo] < from) lo++;
  let hi = lo;
  while (hi < times.length && times[hi] <= to) hi++;
  return [...times.slice(0, lo), ...add, ...times.slice(hi)];
}

/** What the price (or the indicator's pane) shows for a bar, to put a marker against. */
export interface MarkerRef {
  high: number;
  low: number;
  close: number;
}

export interface MarkerPlacement {
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  position: "atPriceTop" | "atPriceBottom" | "atPriceMiddle";
  price: number;
}

const SHAPES = { arrow_up: "arrowUp", arrow_down: "arrowDown", circle: "circle", square: "square" } as const;

/** Above the bar's high, below its low, or on its close ("on"). */
export function markerPlacement(o: MarkerOutput, ref: MarkerRef): MarkerPlacement {
  const shape = SHAPES[o.shape] ?? "circle";
  switch (o.position) {
    case "below":
      return { shape, position: "atPriceBottom", price: ref.low };
    case "on":
      return { shape, position: "atPriceMiddle", price: ref.close };
    default:
      return { shape, position: "atPriceTop", price: ref.high };
  }
}

export interface MarkerData extends MarkerPlacement {
  time: number;
  color: string;
  text?: string;
}

/** The library's markers for the bars (server start times, ascending) an output is on. */
export function buildMarkers(
  o: MarkerOutput,
  times: readonly number[],
  color: string,
  refOf: (t: number) => MarkerRef | undefined,
  toChart: (t: number) => number | undefined,
): MarkerData[] {
  const out: MarkerData[] = [];
  for (const t of times) {
    const time = toChart(t);
    const ref = refOf(t);
    if (time === undefined || !ref) continue;
    out.push({ time, ...markerPlacement(o, ref), color, ...(o.text ? { text: o.text } : {}) });
  }
  return out;
}

/** Chart times of the bars to tint (ascending). */
export function backgroundTimes(times: readonly number[], toChart: (t: number) => number | undefined): number[] {
  const out: number[] = [];
  for (const t of times) {
    const time = toChart(t);
    if (time !== undefined) out.push(time);
  }
  return out;
}

// Fill

/** A band as parallel arrays by chart time; NaN on both sides where either output has no value. */
export interface FillData {
  time: number[];
  upper: Float64Array;
  lower: Float64Array;
}

/** Both outputs' values (by server time, ascending as loaded) joined into a band. */
export function buildFill(
  upper: ReadonlyMap<number, number | null>,
  lower: ReadonlyMap<number, number | null>,
  toChart: (t: number) => number | undefined,
): FillData {
  const time: number[] = [];
  const u: number[] = [];
  const l: number[] = [];
  for (const [t, a] of upper) {
    const x = toChart(t);
    if (x === undefined) continue;
    const b = lower.get(t);
    const gap = a === null || b === null || b === undefined;
    time.push(x);
    u.push(gap ? NaN : a);
    l.push(gap ? NaN : b);
  }
  return { time, upper: Float64Array.from(u), lower: Float64Array.from(l) };
}

/** What the fills and backgrounds of an indicator draw, by output name. */
export interface BandData {
  fills: Map<string, FillData>;
  backgrounds: Map<string, number[]>;
}

/**
 * The data of every fill and background from the values and hits held. It never depends on
 * whether the indicator is shown: a hidden one keeps refreshing, and showing it again must
 * find its data in place.
 */
export function buildBands(
  def: IndicatorDef,
  values: ReadonlyMap<string, ReadonlyMap<number, number | null>>,
  hits: ReadonlyMap<string, readonly number[]>,
  toChart: (t: number) => number | undefined,
): BandData {
  const out: BandData = { fills: new Map(), backgrounds: new Map() };
  for (const o of def.outputs) {
    if (kindOf(o) === "background") out.backgrounds.set(o.name, backgroundTimes(hits.get(o.name) ?? [], toChart));
    const src = fillSources(def, o);
    if (src) out.fills.set(o.name, buildFill(values.get(src[0].name) ?? new Map(), values.get(src[1].name) ?? new Map(), toChart));
  }
  return out;
}

/** The two outputs a fill names, if they are value outputs of the same definition. */
export function fillSources(def: IndicatorDef, o: IndicatorOutput): [ValueOutput, ValueOutput] | undefined {
  if (kindOf(o) !== "fill") return undefined;
  const [a, b] = (o as { between: [string, string] }).between;
  const named = (n: string) => valueOutputs(def).find((v) => v.name === n);
  const upper = named(a);
  const lower = named(b);
  return upper && lower ? [upper, lower] : undefined;
}

/** Where a level is drawn: the server's number for this request, else what the catalog and params give. */
export function levelValue(o: LevelOutput, params: IndicatorParams, levels: Record<string, number> | undefined): number | undefined {
  const answered = levels?.[o.name];
  if (typeof answered === "number" && Number.isFinite(answered)) return answered;
  const own = o.input ? params[o.input] : o.value;
  return typeof own === "number" && Number.isFinite(own) ? own : undefined;
}
