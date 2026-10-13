/**
 * Indicators on the chart (SPEC section 11). Values come from the server, computed on the
 * same bars and price type as the chart; this module only draws them: one line series per
 * output, on the price pane (overlay) or a pane of their own, and a legend row each.
 *
 * Whenever the chart's bars change (load, older history, a rewrite after a reconnect) all
 * values are fetched again for the bars held. While a bar forms, the last two values are
 * refreshed at most once a second.
 */

import { LineSeries, type ISeriesApi, type LineWidth, type UTCTimestamp } from "lightweight-charts";

import {
  ApiError,
  indicatorsApi,
  type IndicatorDef,
  type IndicatorEntry,
  type IndicatorParams,
} from "../api";
import { button, h, icon, menuItem, showMenu } from "../ui/dom";
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
  series: Map<string, ISeriesApi<"Line">>;
  values: Map<string, Map<number, number>>; // output -> bar time -> value
  row: HTMLElement;
  valueEls: Map<string, HTMLElement>;
  generation: number;
  error?: string;
}

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
      row: h("div", { class: "legend-row" }),
      valueEls: new Map(),
      generation: 0,
    };
    const pane = def.overlay ? 0 : 1 + this.active.filter((x) => !x.def.overlay).length;
    for (const o of def.outputs) {
      a.series.set(o.name, this.view.chart.addSeries(LineSeries, { priceLineVisible: false, crosshairMarkerRadius: 3 }, pane));
      a.values.set(o.name, new Map());
    }
    this.active.push(a);
    this.style(a);
    this.renderRow(a);
    this.view.legendIndicators.append(a.row);
    return a;
  }

  private dispose(a: Active): void {
    a.generation++;
    for (const s of a.series.values()) this.view.chart.removeSeries(s);
    a.row.remove();
  }

  /** Overlays on the price pane; the others one pane each, in order, with no empty pane left. */
  private layoutPanes(): void {
    let pane = 1;
    for (const a of this.active) {
      if (a.def.overlay) continue;
      for (const s of a.series.values()) if (s.getPane().paneIndex() !== pane) s.moveToPane(pane);
      pane++;
    }
    const panes = this.view.chart.panes();
    for (let i = panes.length - 1; i >= pane; i--) {
      if (!panes[i].getSeries().length) this.view.chart.removePane(i);
    }
    // The price pane keeps most of the height: each indicator pane gets a quarter of it.
    this.view.chart.panes().forEach((p, i) => p.setStretchFactor(i === 0 ? 4 : 1));
  }

  private style(a: Active): void {
    for (const o of a.def.outputs) {
      a.series.get(o.name)!.applyOptions({
        color: String(a.entry.params[o.color]),
        lineWidth: Number(a.entry.params[o.width]) as LineWidth,
        visible: a.entry.visible,
        lastValueVisible: a.entry.price_labels,
        title: "",
      });
    }
  }

  private find(uid: number): Active | undefined {
    return this.active.find((a) => a.uid === uid);
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

  /** Values for every bar held. */
  private async load(a: Active): Promise<void> {
    const span = this.view.span();
    const q = this.query(a);
    const gen = ++a.generation;
    if (!span || !q) {
      for (const s of a.series.values()) s.setData([]);
      return;
    }
    const got = new Map<string, Map<number, number>>(a.def.outputs.map((o) => [o.name, new Map()]));
    try {
      let from = span.first;
      for (;;) {
        const page = await indicatorsApi.values(a.def.id, { ...q, from, to: span.last + 1, limit: PAGE });
        if (gen !== a.generation) return;
        for (const [name, values] of Object.entries(page.values)) {
          const m = got.get(name);
          if (m) page.t.forEach((t, i) => values[i] !== null && m.set(t, values[i]!));
        }
        if (page.t.length < PAGE) break;
        from = page.t[page.t.length - 1] + 1;
      }
    } catch (err) {
      if (gen === a.generation) this.fail(a, err);
      return;
    }
    a.error = undefined;
    a.values = got;
    for (const [name, m] of got) {
      const data = [];
      for (const [t, value] of m) {
        const time = this.view.chartTimeOf(t);
        if (time !== undefined) data.push({ time: time as UTCTimestamp, value });
      }
      a.series.get(name)!.setData(data);
    }
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
      const lastShown = new Map([...a.series].map(([name, s]) => [name, s.data().at(-1)?.time as number | undefined]));
      for (const [name, values] of Object.entries(page.values)) {
        const series = a.series.get(name);
        const m = a.values.get(name);
        if (!series || !m) continue;
        page.t.forEach((t, i) => {
          const value = values[i];
          const time = this.view.chartTimeOf(t);
          if (value === null || time === undefined) return;
          m.set(t, value);
          const last = lastShown.get(name);
          series.update({ time: time as UTCTimestamp, value }, last !== undefined && time < last);
        });
      }
      this.renderValues(a);
    } catch {
      /* the next update or reload tries again */
    }
  }

  private fail(a: Active, err: unknown): void {
    a.error = err instanceof ApiError ? err.message : String(err);
    for (const s of a.series.values()) s.setData([]);
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
    a.valueEls = new Map(a.def.outputs.map((o) => [o.name, h("b", { class: "num" })]));
    const values = h(
      "span",
      { class: "legend-row-values", hidden: !a.entry.legend_values || !a.entry.visible },
      ...a.def.outputs.map((o) => {
        const el = a.valueEls.get(o.name)!;
        el.style.color = String(a.entry.params[o.color]);
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
      el.textContent = value === undefined ? "∅" : this.view.formatPriceValue(value);
    }
  }
}
