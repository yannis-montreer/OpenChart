/**
 * Indicators on the chart (SPEC section 11). Values come from the server, computed on the
 * same bars and price type as the chart; this module only draws them, on the price pane
 * (overlay) or a pane of their own, with a legend row each. What each output kind becomes
 * (CI-2 (d)):
 *
 * - `line`, `histogram`, `area`: a series of that type, with the colour, width and line
 *   style the catalog names;
 * - `fill`: a primitive painting the band between two value outputs;
 * - `level`: a price line at the number in the answer;
 * - `marker`: the library's markers at the bars in `hits`, against the bar's high, low or
 *   close on the price pane, against the line's value in a pane of its own;
 * - `background`: a primitive tinting the columns of the bars in `hits`.
 *
 * Fills, levels, markers and backgrounds hang on one series per indicator (its first value
 * output, or a hidden one holding the closes when it has none), so they follow it between
 * panes and go when it goes. `indicatorOutputs.ts` holds the mapping from the answer to
 * their data and `indicatorPrimitives.ts` the two primitives.
 *
 * Whenever the chart's bars change (load, older history, a rewrite after a reconnect) all
 * values are fetched again for the bars held. While a bar forms, the last two bars are
 * refreshed at most once a second.
 */

import {
  AreaSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineWidth,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

import {
  ApiError,
  indicatorsApi,
  type IndicatorDef,
  type IndicatorEntry,
  type IndicatorParams,
  type IndicatorValues,
  type LevelOutput,
  type MarkerOutput,
} from "../api";
import { button, h, icon, menuItem, showMenu } from "../ui/dom";
import {
  buildBands,
  buildMarkers,
  fillSources,
  hitTimes,
  kindOf,
  levelValue,
  lineData,
  lineStyleCode,
  paramColor,
  paramWidth,
  replaceWindow,
  seriesPlan,
  tailPoints,
  valueOutputs,
  withOpacity,
  type MarkerRef,
} from "./indicatorOutputs";
import { BackgroundPrimitive, FillPrimitive } from "./indicatorPrimitives";
import type { ChartView } from "./view";

const PAGE = 5000; // the server's largest page
const LIVE_EVERY_MS = 1000;

/** "EMA 9 close": the name and the input values (not the style ones), like a legend shows. */
export function indicatorLabel(def: IndicatorDef, params: IndicatorParams): string {
  const inputs = def.params.filter((p) => p.group === "inputs").map((p) => String(params[p.name] ?? p.default));
  return [def.name, ...inputs].join(" ");
}

/** A new entry for `def`, with its default parameters. */
export function newEntry(def: IndicatorDef): IndicatorEntry {
  const params: IndicatorParams = {};
  for (const p of def.params) params[p.name] = p.default;
  return { id: def.id, params, visible: true, price_labels: true, legend_values: true };
}

/** Parameters the computation depends on (a style change needs no new values). */
export function inputKey(def: IndicatorDef, params: IndicatorParams): string {
  return JSON.stringify(def.params.filter((p) => p.group === "inputs").map((p) => params[p.name]));
}

interface Active {
  uid: number;
  def: IndicatorDef;
  entry: IndicatorEntry;
  /** One series per output that has values (line, histogram, area). */
  series: Map<string, ISeriesApi<SeriesType>>;
  /** Hidden series holding the closes, only for an indicator with no value output of its own
   * that still has markers, levels or backgrounds to hang on a series. */
  anchor?: ISeriesApi<"Line">;
  values: Map<string, Map<number, number | null>>; // output -> bar time -> value
  hits: Map<string, number[]>; // marker and background outputs -> start times of the bars they are on
  levels: Record<string, number>;
  markers: Map<string, ISeriesMarkersPluginApi<Time>>;
  fills: Map<string, { primitive: FillPrimitive; series: ISeriesApi<SeriesType> }>;
  backgrounds: Map<string, BackgroundPrimitive>;
  priceLines: IPriceLine[];
  row: HTMLElement;
  valueEls: Map<string, HTMLElement>;
  generation: number;
  error?: string;
}

const NO_FILL = { time: [], upper: new Float64Array(0), lower: new Float64Array(0) };

export class IndicatorLayer {
  private catalog = new Map<string, IndicatorDef>();
  private active: Active[] = [];
  private nextUid = 1;
  private liveTimer?: number;
  private hoverTime?: number;
  /** The list changed (added, removed, edited): the app saves it. */
  onChange: () => void = () => {};
  /** "Settings…" on an indicator's legend row. */
  onSettings: (uid: number) => void = () => {};

  constructor(private view: ChartView) {
    view.onBars = (kind) => (kind === "history" ? this.reloadAll() : this.scheduleLive());
    view.onHover = (t) => this.hover(t);
    // A new price series (another chart type) shows other highs and lows to put markers against;
    // the view has not drawn it yet when it tells us.
    view.onSeriesChange(() => window.setTimeout(() => this.active.forEach((a) => this.paintMarkers(a)), 0));
  }

  setCatalog(defs: IndicatorDef[]): void {
    this.catalog = new Map(defs.map((d) => [d.id, d]));
  }

  definitions(): IndicatorDef[] {
    return [...this.catalog.values()];
  }

  /** What a layout stores. */
  entries(): IndicatorEntry[] {
    return this.active.map((a) => structuredClone(a.entry));
  }

  /** Replace every indicator (a layout was loaded). Unknown ids are skipped. Values are
   * fetched now, or with `reload` false when the chart is about to load anyway. */
  setEntries(entries: IndicatorEntry[], reload = true): void {
    for (const a of [...this.active]) this.dispose(a);
    this.active = [];
    for (const e of entries) {
      const def = this.catalog.get(e.id);
      if (def) this.create(def, { ...newEntry(def), ...e, params: { ...newEntry(def).params, ...e.params } });
    }
    this.layoutPanes();
    if (reload) this.reloadAll();
  }

  add(def: IndicatorDef): number {
    const a = this.create(def, newEntry(def));
    this.layoutPanes();
    void this.load(a);
    this.onChange();
    return a.uid;
  }

  remove(uid: number): void {
    const a = this.find(uid);
    if (!a) return;
    this.dispose(a);
    this.active = this.active.filter((x) => x !== a);
    this.layoutPanes();
    this.onChange();
  }

  get(uid: number): { def: IndicatorDef; entry: IndicatorEntry } | undefined {
    const a = this.find(uid);
    return a && { def: a.def, entry: structuredClone(a.entry) };
  }

  /** New parameters or display options; values are fetched again only if inputs changed. */
  update(uid: number, entry: IndicatorEntry, save = true): void {
    const a = this.find(uid);
    if (!a) return;
    const refetch = inputKey(a.def, a.entry.params) !== inputKey(a.def, entry.params);
    a.entry = structuredClone(entry);
    this.style(a);
    this.renderRow(a);
    if (refetch) void this.load(a);
    if (save) this.onChange();
  }

  /** Show the values of the bar at server time `t` in the legend (undefined: the last bar). */
  hover(t: number | undefined): void {
    this.hoverTime = t;
    for (const a of this.active) this.renderValues(a);
  }

  // Series and panes

  private create(def: IndicatorDef, entry: IndicatorEntry): Active {
    const a: Active = {
      uid: this.nextUid++,
      def,
      entry,
      series: new Map(),
      values: new Map(),
      hits: new Map(),
      levels: {},
      markers: new Map(),
      fills: new Map(),
      backgrounds: new Map(),
      priceLines: [],
      row: h("div", { class: "legend-row" }),
      valueEls: new Map(),
      generation: 0,
    };
    const chart = this.view.chart;
    const pane = def.overlay ? 0 : 1 + this.active.filter((x) => !x.def.overlay).length;
    for (const plan of seriesPlan(def, entry)) {
      const kind = plan.kind === "histogram" ? HistogramSeries : plan.kind === "area" ? AreaSeries : LineSeries;
      a.series.set(plan.name, chart.addSeries(kind, plan.create as never, pane));
      a.values.set(plan.name, new Map());
    }
    const hung = def.outputs.some((o) => ["marker", "level", "background"].includes(kindOf(o)));
    if (hung && !a.series.size) {
      a.anchor = chart.addSeries(
        LineSeries,
        {
          lineVisible: false,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          title: "",
          // On the price pane the closes lie inside its range anyway; do not let them steer it.
          ...(def.overlay ? { autoscaleInfoProvider: () => null } : {}),
        },
        pane,
      );
    }
    const host = this.host(a);
    for (const o of def.outputs) {
      switch (kindOf(o)) {
        case "marker":
          a.hits.set(o.name, []);
          if (host) a.markers.set(o.name, createSeriesMarkers(host, []));
          break;
        case "background": {
          a.hits.set(o.name, []);
          const primitive = new BackgroundPrimitive();
          host?.attachPrimitive(primitive);
          a.backgrounds.set(o.name, primitive);
          break;
        }
        case "fill": {
          const src = fillSources(def, o);
          const series = src && a.series.get(src[0].name);
          if (series) {
            const primitive = new FillPrimitive(series);
            series.attachPrimitive(primitive);
            a.fills.set(o.name, { primitive, series });
          }
          break;
        }
      }
    }
    this.active.push(a);
    this.style(a);
    this.renderRow(a);
    this.view.legendIndicators.append(a.row);
    return a;
  }

  /** The series the indicator's fills, levels, markers and backgrounds hang on. */
  private host(a: Active): ISeriesApi<SeriesType> | undefined {
    return a.anchor ?? a.series.values().next().value;
  }

  private allSeries(a: Active): ISeriesApi<SeriesType>[] {
    return [...a.series.values(), ...(a.anchor ? [a.anchor] : [])];
  }

  private dispose(a: Active): void {
    a.generation++;
    this.removePriceLines(a);
    for (const m of a.markers.values()) m.detach();
    const host = this.host(a);
    for (const p of a.backgrounds.values()) host?.detachPrimitive(p);
    for (const f of a.fills.values()) f.series.detachPrimitive(f.primitive);
    for (const s of this.allSeries(a)) this.view.chart.removeSeries(s);
    a.row.remove();
  }

  /** Overlays on the price pane; the others one pane each, in order, with no empty pane left. */
  private layoutPanes(): void {
    let pane = 1;
    for (const a of this.active) {
      if (a.def.overlay) continue;
      for (const s of this.allSeries(a)) if (s.getPane().paneIndex() !== pane) s.moveToPane(pane);
      pane++;
    }
    const panes = this.view.chart.panes();
    for (let i = panes.length - 1; i >= pane; i--) {
      if (!panes[i].getSeries().length) this.view.chart.removePane(i);
    }
    // The price pane keeps most of the height: each indicator pane gets a quarter of it.
    this.view.chart.panes().forEach((p, i) => p.setStretchFactor(i === 0 ? 4 : 1));
  }

  /** Colours, widths, styles and visibility from the entry, for every output kind. */
  private style(a: Active): void {
    for (const plan of seriesPlan(a.def, a.entry)) a.series.get(plan.name)!.applyOptions(plan.options as never);
    this.paintMarkers(a);
    this.paintLevels(a);
    this.paintBands(a, false);
  }

  private find(uid: number): Active | undefined {
    return this.active.find((a) => a.uid === uid);
  }

  // Outputs that are not series

  private shown(a: Active): boolean {
    return a.entry.visible && !a.error;
  }

  private paintMarkers(a: Active): void {
    if (!a.markers.size) return;
    const toChart = (t: number) => this.view.chartTimeOf(t);
    const ref = this.markerRef(a);
    for (const o of a.def.outputs) {
      const plugin = a.markers.get(o.name);
      if (!plugin) continue;
      const times = this.shown(a) ? (a.hits.get(o.name) ?? []) : [];
      const built = buildMarkers(o as MarkerOutput, times, paramColor(a.entry.params, o.color), ref, toChart);
      plugin.setMarkers(built as unknown as SeriesMarker<Time>[]);
    }
  }

  /** What a marker sits against: the bar on the price pane, the line's value in a pane of its own. */
  private markerRef(a: Active): (t: number) => MarkerRef | undefined {
    if (a.def.overlay || !a.series.size) return (t) => this.view.displayedRange(t);
    const maps = valueOutputs(a.def).map((o) => a.values.get(o.name));
    return (t) => {
      for (const m of maps) {
        const v = m?.get(t);
        if (v !== undefined && v !== null) return { high: v, low: v, close: v };
      }
      return undefined;
    };
  }

  private removePriceLines(a: Active): void {
    const host = this.host(a);
    for (const line of a.priceLines) host?.removePriceLine(line);
    a.priceLines = [];
  }

  private paintLevels(a: Active): void {
    this.removePriceLines(a);
    const host = this.host(a);
    if (!host || !this.shown(a)) return;
    for (const o of a.def.outputs) {
      if (kindOf(o) !== "level") continue;
      const l = o as LevelOutput;
      const price = levelValue(l, a.entry.params, a.levels);
      if (price === undefined) continue;
      a.priceLines.push(
        host.createPriceLine({
          price,
          color: paramColor(a.entry.params, l.color),
          lineWidth: paramWidth(a.entry.params, l.width, 1) as LineWidth,
          lineStyle: lineStyleCode(l.style),
          axisLabelVisible: a.entry.price_labels,
          title: "",
        }),
      );
    }
  }

  /**
   * Fills and backgrounds: colour and visibility always, their data only when `data` (it was
   * fetched). The data is built whether or not the indicator is shown: a hidden one keeps
   * refreshing, and showing it again only switches the primitives on.
   */
  private paintBands(a: Active, data: boolean): void {
    const show = this.shown(a);
    const built = data ? buildBands(a.def, a.values, a.hits, (t) => this.view.chartTimeOf(t)) : undefined;
    for (const o of a.def.outputs) {
      const color = paramColor(a.entry.params, o.color);
      const fill = a.fills.get(o.name);
      if (fill && o.kind === "fill") {
        if (built) fill.primitive.setData(built.fills.get(o.name) ?? NO_FILL);
        fill.primitive.setColor(withOpacity(color, o.opacity));
        fill.primitive.setVisible(show);
      }
      const bg = a.backgrounds.get(o.name);
      if (bg && o.kind === "background") {
        if (built) bg.setTimes(built.backgrounds.get(o.name) ?? []);
        bg.setColor(withOpacity(color, o.opacity));
        bg.setVisible(show);
      }
    }
  }

  // Values

  private reloadAll(): void {
    for (const a of this.active) void this.load(a);
  }

  private scheduleLive(): void {
    if (this.liveTimer !== undefined || !this.active.length) return;
    this.liveTimer = window.setTimeout(() => {
      this.liveTimer = undefined;
      for (const a of this.active) void this.loadTail(a);
    }, LIVE_EVERY_MS);
  }

  private query(a: Active) {
    const ctx = this.view.ctx;
    if (!ctx) return undefined;
    return { instrument: ctx.instrument.instrument, timeframe: ctx.timeframe.name, price: ctx.price, params: a.entry.params };
  }

  /** Nothing to draw: no data in any series, no hits, no levels. */
  private clear(a: Active): void {
    for (const s of this.allSeries(a)) s.setData([]);
    a.values = new Map(valueOutputs(a.def).map((o) => [o.name, new Map()]));
    a.hits = new Map([...a.hits.keys()].map((n) => [n, []]));
    a.levels = {};
    this.paintMarkers(a);
    this.paintLevels(a);
    this.paintBands(a, true);
  }

  /** Values, hits and levels for every bar held. */
  private async load(a: Active): Promise<void> {
    const span = this.view.span();
    const q = this.query(a);
    const gen = ++a.generation;
    if (!span || !q) {
      this.clear(a);
      return;
    }
    const got = new Map<string, Map<number, number | null>>(valueOutputs(a.def).map((o) => [o.name, new Map()]));
    const hits = new Map<string, number[]>([...a.hits.keys()].map((n) => [n, []]));
    let levels: Record<string, number> = {};
    try {
      let from = span.first;
      for (;;) {
        const page = await indicatorsApi.values(a.def.id, { ...q, from, to: span.last + 1, limit: PAGE });
        if (gen !== a.generation) return;
        for (const [name, values] of Object.entries(page.values)) {
          const m = got.get(name);
          if (m) page.t.forEach((t, i) => m.set(t, values[i] ?? null));
        }
        for (const [name, list] of hits) for (const t of hitTimes(page.hits?.[name], page.t)) list.push(t);
        if (page.levels) levels = page.levels;
        if (page.t.length < PAGE) break;
        from = page.t[page.t.length - 1] + 1;
      }
    } catch (err) {
      if (gen === a.generation) this.fail(a, err);
      return;
    }
    a.error = undefined;
    a.values = got;
    a.hits = hits;
    a.levels = levels;
    const toChart = (t: number) => this.view.chartTimeOf(t);
    for (const [name, m] of got) a.series.get(name)!.setData(lineData(m, toChart) as { time: UTCTimestamp; value: number }[]);
    a.anchor?.setData(this.view.closeData() as { time: UTCTimestamp; value: number }[]);
    this.paintMarkers(a);
    this.paintLevels(a);
    this.paintBands(a, true);
    this.renderRow(a);
  }

  /** The forming bar and the one before it (it may just have closed). */
  private async loadTail(a: Active): Promise<void> {
    const span = this.view.span();
    const q = this.query(a);
    if (!span || !q || a.error) return;
    const gen = a.generation;
    try {
      const page = await indicatorsApi.values(a.def.id, { ...q, from: span.beforeLast ?? span.last, to: span.last + 1 });
      if (gen !== a.generation) return;
      this.applyTail(a, page, span.count);
      this.renderValues(a);
    } catch {
      /* the next update or reload tries again */
    }
  }

  private applyTail(a: Active, page: IndicatorValues, count: number): void {
    const lastShown = new Map([...a.series].map(([name, s]) => [name, s.data().at(-1)?.time as number | undefined]));
    for (const [name, values] of Object.entries(page.values)) {
      const series = a.series.get(name);
      const m = a.values.get(name);
      if (!series || !m) continue;
      for (const p of tailPoints(page.t, values, (t) => this.view.chartTimeOf(t))) {
        m.set(p.t, p.value);
        const last = lastShown.get(name);
        series.update({ time: p.time as UTCTimestamp, value: p.value } as never, last !== undefined && p.time < last);
      }
    }
    if (a.anchor) {
      const last = a.anchor.data().at(-1)?.time as number | undefined;
      for (const p of this.view.closeData(Math.max(0, count - 2))) a.anchor.update(p as { time: UTCTimestamp; value: number }, last !== undefined && p.time < last);
    }
    const from = page.t[0];
    const to = page.t[page.t.length - 1];
    if (from !== undefined && a.hits.size) {
      for (const [name, times] of a.hits) a.hits.set(name, replaceWindow(times, from, to, hitTimes(page.hits?.[name], page.t)));
    }
    if (page.levels) a.levels = page.levels;
    if (a.markers.size) this.paintMarkers(a);
    if (a.fills.size || a.backgrounds.size) this.paintBands(a, true);
  }

  private fail(a: Active, err: unknown): void {
    a.error = err instanceof ApiError ? err.message : String(err);
    this.clear(a);
    this.renderRow(a);
  }

  // Legend

  private renderRow(a: Active): void {
    const label = indicatorLabel(a.def, a.entry.params);
    const eye = button({
      icon: a.entry.visible ? "eye" : "eyeOff",
      title: a.entry.visible ? "Hide" : "Show",
      onClick: () => this.update(a.uid, { ...a.entry, visible: !a.entry.visible }),
    });
    const more = button({ icon: "more", title: "More" });
    more.addEventListener("click", () =>
      showMenu(
        more,
        h(
          "div",
          { style: "min-width:200px" },
          menuItem({ label: "Settings…", icon: "settings", onClick: () => this.onSettings(a.uid) }),
          menuItem({
            label: a.entry.visible ? "Hide" : "Show",
            icon: a.entry.visible ? "eyeOff" : "eye",
            onClick: () => this.update(a.uid, { ...a.entry, visible: !a.entry.visible }),
          }),
          h("hr"),
          menuItem({ label: "Remove", icon: "trash", onClick: () => this.remove(a.uid) }),
        ),
      ),
    );
    const outputs = valueOutputs(a.def);
    a.valueEls = new Map(outputs.map((o) => [o.name, h("b", { class: "num" })]));
    const values = h(
      "span",
      { class: "legend-row-values", hidden: !a.entry.legend_values || !a.entry.visible },
      ...outputs.map((o) => {
        const el = a.valueEls.get(o.name)!;
        el.style.color = paramColor(a.entry.params, o.color);
        return el;
      }),
    );
    a.row.className = `legend-row${a.entry.visible ? "" : " off"}${a.error ? " error" : ""}`;
    a.row.replaceChildren(
      h("span", { class: "legend-row-name", "data-tip": a.def.title }, label),
      values,
      ...(a.error ? [h("span", { class: "legend-row-error", "data-tip": a.error }, icon("warn", 14), "Could not compute")] : []),
      h(
        "span",
        { class: "legend-row-actions" },
        eye,
        button({ icon: "settings", title: "Settings", onClick: () => this.onSettings(a.uid) }),
        button({ icon: "trash", title: "Remove", onClick: () => this.remove(a.uid) }),
        more,
      ),
    );
    a.row.ondblclick = (ev) => {
      if (!(ev.target as Element).closest("button")) this.onSettings(a.uid);
    };
    this.renderValues(a);
  }

  private renderValues(a: Active): void {
    const t = this.hoverTime ?? this.view.span()?.last;
    for (const [name, el] of a.valueEls) {
      const value = t === undefined ? undefined : a.values.get(name)?.get(t);
      el.textContent = value === undefined || value === null ? "∅" : this.view.formatPriceValue(value);
    }
  }
}
