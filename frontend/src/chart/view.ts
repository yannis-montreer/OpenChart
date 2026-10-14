/**
 * The price chart: history from REST, live updates from the websocket.
 *
 * Bars are kept with their server time `t` (UTC bar start) next to the chart time used
 * by Lightweight Charts (see time.ts). Scrolling left loads older history. After a
 * dropped websocket, `resync` fetches bars from the last one we have and applies them as
 * updates, so the chart catches up without being rebuilt.
 */

import {
  AreaSeries,
  BarSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

import { api, type Bar, type ChartType, type Instrument, type Price, type TimeframeMeta } from "../api";
import { haStep, heikinAshi } from "./heikinAshi";
import { h } from "../ui/dom";
import { mergeBars } from "./merge";
import { TimeIndex, wallAdvance } from "../drawings/geometry";
import type { Clock } from "../drawings/tradingTime";
import {
  formatChartTime,
  formatCountdown,
  toChartTimes,
  type SessionMeta,
} from "../time";

export interface ChartContext {
  instrument: Instrument;
  timeframe: TimeframeMeta;
  price: Price;
  tz: string;
  session: SessionMeta;
}

const PAGE = 1000;
const MAX_PAGE = 5000;

interface Palette {
  chart: string;
  grid: string;
  text: string;
  muted: string;
  line: string;
  crosshair: string;
  up: string;
  down: string;
  bid: string;
  ask: string;
  raised: string;
  accent: string;
}

function readPalette(): Palette {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(`--${name}`).trim();
  return {
    chart: v("chart"),
    grid: v("grid"),
    text: v("text"),
    muted: v("muted"),
    line: v("line-strong"),
    crosshair: v("crosshair"),
    up: v("up"),
    down: v("down"),
    bid: v("bid"),
    ask: v("ask"),
    raised: v("active"),
    accent: v("accent"),
  };
}

export const CHART_TYPES: { id: ChartType; label: string }[] = [
  { id: "candles", label: "Candles" },
  { id: "bars", label: "Bars" },
  { id: "line", label: "Line" },
  { id: "area", label: "Area" },
  { id: "heikin_ashi", label: "Heikin Ashi" },
];

/** "#rrggbb" plus alpha as rgba() (the chart library does not parse CSS color-mix). */
export function withAlpha(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return hex;
  const [r, g, b] = m.slice(1).map((x) => parseInt(x, 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export class ChartView {
  readonly chart: IChartApi;
  /** The price series; its type follows the chart type (candles, bars, line, area, Heikin Ashi). */
  private main: ISeriesApi<SeriesType>;
  chartType: ChartType = "candles";
  /** Heikin Ashi bars for the bars on screen (only while that chart type is shown). */
  private ha: Bar[] = [];
  /** With Heikin Ashi: the real last price on the price scale (the series' own is hidden). */
  private realLine?: IPriceLine;
  private seriesListeners: ((before: ISeriesApi<SeriesType>, after: ISeriesApi<SeriesType>) => void)[] = [];
  private volume: ISeriesApi<"Histogram">;
  private bidLine?: IPriceLine;
  private askLine?: IPriceLine;
  private palette = readPalette();

  ctx?: ChartContext;
  private bars: Bar[] = [];
  private times: number[] = [];
  private generation = 0;
  private loadingOlder = false;
  private exhausted = false;
  private hovering = false;
  private formatPrice: (x: number) => string = (x) => String(x);
  /** Live updates that arrive while a load is in flight; applied after it. */
  private pending: Bar[] | null = null;
  private lastTick?: { bid: number; ask: number };

  private legendTitle: HTMLElement;
  private legendValues: HTMLElement;
  private countdown: HTMLElement;
  private askTag: HTMLElement;
  private bidTag: HTMLElement;
  private message: HTMLElement;
  /** Indicator rows under the legend (filled by the indicators). */
  readonly legendIndicators: HTMLElement;

  /** Bars changed: "history" (loaded, older bars added, rewritten) or "live" (one bar updated). */
  onBars: (kind: "history" | "live") => void = () => {};
  /** The crosshair is on the bar starting at server time `t` (undefined: it left the chart). */
  onHover: (t: number | undefined) => void = () => {};
  /** Shown in the legend; set by the app. */
  feedDotClass = "dot";
  /** Called with a short error text when loading fails. */
  onError: (text: string) => void = () => {};

  constructor(area: HTMLElement) {
    const host = h("div", { class: "chart-host" });
    this.legendTitle = h("div", { class: "legend-title" });
    this.legendValues = h("div", { class: "legend-values num" });
    this.countdown = h("div", { class: "quote-tag countdown num", hidden: true });
    this.askTag = h("div", { class: "quote-tag ask num", hidden: true }, h("span", { class: "side" }, "Ask"), h("span"));
    this.bidTag = h("div", { class: "quote-tag bid num", hidden: true }, h("span", { class: "side" }, "Bid"), h("span"));
    this.message = h("div", { class: "chart-message", hidden: true });
    this.legendIndicators = h("div", { class: "legend-indicators" });
    area.append(
      host,
      h("div", { class: "legend" }, this.legendTitle, this.legendValues, this.legendIndicators),
      this.countdown,
      this.askTag,
      this.bidTag,
      this.message,
    );

    this.chart = createChart(host, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: this.palette.chart },
        textColor: this.palette.muted,
        fontSize: 11,
        fontFamily: getComputedStyle(document.body).fontFamily,
        attributionLogo: true, // required attribution for TradingView Lightweight Charts
        panes: { separatorColor: this.palette.line },
      },
      grid: {
        vertLines: { color: this.palette.grid },
        horzLines: { color: this.palette.grid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: this.palette.crosshair, style: LineStyle.Dashed, labelBackgroundColor: this.palette.raised },
        horzLine: { color: this.palette.crosshair, style: LineStyle.Dashed, labelBackgroundColor: this.palette.raised },
      },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: {
        borderVisible: false,
        rightOffset: 10,
        barSpacing: 7,
        minBarSpacing: 0.6,
        timeVisible: true,
        secondsVisible: false,
      },
      localization: {
        locale: "en-GB",
        timeFormatter: (t: Time) => formatChartTime(t as number, this.ctx?.timeframe.name ?? "1m"),
      },
    });

    this.volume = this.chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    this.chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
    this.main = this.makeMain("candles");

    this.chart.subscribeCrosshairMove((p) => this.onCrosshair(p));
    this.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range && range.from < 40) void this.loadOlder();
      this.placeQuotes();
    });
    window.setInterval(() => this.placeQuotes(), 1000);
  }

  // Loading

  async load(ctx: ChartContext, opts: { from?: number; visible?: [number, number] } = {}): Promise<void> {
    const gen = ++this.generation;
    // Another instrument has its own price range: a price scale the user dragged on
    // gold (around 2650) would show nothing of EUR/USD (around 1.08).
    if (this.ctx && this.ctx.instrument.instrument !== ctx.instrument.instrument) {
      this.chart.priceScale("right").applyOptions({ autoScale: true });
    }
    this.ctx = ctx;
    this.exhausted = false;
    this.lastTick = undefined;
    this.removeQuoteLines();
    this.applyFormat();
    this.chart.applyOptions({ timeScale: { secondsVisible: ctx.timeframe.kind === "tick" } });
    this.showMessage("Loading…");
    this.pending = [];
    let bars: Bar[];
    try {
      bars = await api.candles({
        instrument: ctx.instrument.instrument,
        timeframe: ctx.timeframe.name,
        price: ctx.price,
        from: opts.from,
        limit: opts.from ? MAX_PAGE : PAGE,
      });
    } catch (err) {
      if (gen === this.generation) {
        this.pending = null;
        this.showMessage(`Could not load candles: ${(err as Error).message}`);
        this.onError((err as Error).message);
      }
      return;
    }
    if (gen !== this.generation) return; // a newer load started meanwhile
    this.setBars(bars);
    const queued = this.pending ?? [];
    this.pending = null;
    for (const bar of queued) this.applyBar(bar);
    if (opts.visible) {
      const [a, b] = opts.visible;
      this.chart.timeScale().setVisibleRange({ from: this.toChart(a) as Time, to: this.toChart(b) as Time });
    } else {
      this.chart.timeScale().resetTimeScale();
      this.chart.timeScale().scrollToRealTime();
    }
    this.showMessage(
      bars.length
        ? null
        : ctx.timeframe.kind === "tick"
          ? "No tick data yet for this timeframe. Second bars are built from ticks recorded while the server runs."
          : "No candles stored for this instrument yet.",
    );
  }

  /** Re-render existing bars, for example after the display timezone changed. */
  rerender(ctx: ChartContext): void {
    this.ctx = ctx;
    const range = this.chart.timeScale().getVisibleLogicalRange();
    this.setBars(this.bars);
    if (range) this.chart.timeScale().setVisibleLogicalRange(range);
  }

  private setBars(bars: Bar[]): void {
    const ctx = this.ctx!;
    const mapped = toChartTimes(bars, ctx.timeframe.name, ctx.tz, ctx.session);
    this.bars = mapped.bars;
    this.times = mapped.times;
    this.renderSeries();
    this.renderLegend();
    this.placeQuotes();
    this.onBars("history");
  }

  /** Data of the price and volume series from the bars we hold. */
  private renderSeries(): void {
    this.ha = this.chartType === "heikin_ashi" ? heikinAshi(this.bars) : [];
    this.main.setData(this.bars.map((_, i) => this.point(i, this.times[i])));
    this.volume.setData(this.bars.map((b, i) => this.volumeData(b, this.times[i])));
    this.updateRealLine();
  }

  private async loadOlder(): Promise<void> {
    if (!this.ctx || this.loadingOlder || this.exhausted || !this.bars.length) return;
    const ctx = this.ctx;
    const gen = this.generation;
    this.loadingOlder = true;
    try {
      const older = await api.candles({
        instrument: ctx.instrument.instrument,
        timeframe: ctx.timeframe.name,
        price: ctx.price,
        to: this.bars[0].t,
        limit: PAGE,
      });
      if (gen !== this.generation) return;
      if (!older.length) {
        this.exhausted = true;
        return;
      }
      const range = this.chart.timeScale().getVisibleLogicalRange();
      const before = this.bars.length;
      this.setBars([...older, ...this.bars]);
      const added = this.bars.length - before;
      if (range) {
        this.chart.timeScale().setVisibleLogicalRange({ from: range.from + added, to: range.to + added });
      }
    } catch (err) {
      console.warn("loading older candles failed", err);
    } finally {
      this.loadingOlder = false;
    }
  }

  /**
   * Catch up after a websocket reconnect, without reloading the page.
   *
   * Re-fetches from a few bars before our last one and merges: new bars are applied as
   * live updates; a bar that formed while we were away (or a corrected one) is inserted
   * and the series redrawn in place, keeping the view where it was.
   */
  async resync(lookbackBars = 10): Promise<void> {
    if (!this.ctx) return;
    if (!this.bars.length) return this.load(this.ctx);
    const ctx = this.ctx;
    const gen = this.generation;
    const n = this.bars.length;
    const from = this.bars[Math.max(0, n - 1 - lookbackBars)].t;
    const fresh = await api.candles({
      instrument: ctx.instrument.instrument,
      timeframe: ctx.timeframe.name,
      price: ctx.price,
      from,
      limit: MAX_PAGE,
    });
    if (gen !== this.generation || this.pending) return;
    if (fresh.length >= MAX_PAGE) return this.load(ctx); // too far behind: start over
    const merged = mergeBars(this.bars, fresh);
    if (!merged.rewritesHistory) {
      for (const bar of merged.tail) this.applyBar(bar);
      return;
    }
    // Keep the same distance from the right edge while bars are inserted.
    const ts = this.chart.timeScale();
    const range = ts.getVisibleLogicalRange();
    const before = this.bars.length;
    this.setBars(merged.bars);
    const added = this.bars.length - before;
    if (range) ts.setVisibleLogicalRange({ from: range.from + added, to: range.to + added });
  }

  // Live updates

  applyBar(bar: Bar): void {
    const ctx = this.ctx;
    if (!ctx) return;
    if (this.pending) {
      this.pending.push(bar);
      return;
    }
    const n = this.bars.length;
    const last = n ? this.bars[n - 1] : undefined;
    let ct: number;
    if (last && bar.t === last.t) {
      ct = this.times[n - 1];
      this.bars[n - 1] = bar;
    } else if (!last || bar.t > last.t) {
      ct = this.toChart(bar.t);
      if (n && ct <= this.times[n - 1]) return;
      this.bars.push(bar);
      this.times.push(ct);
      if (n === 0) this.showMessage(null);
    } else {
      return; // older than what we show; history already has it
    }
    const i = this.bars.length - 1;
    if (this.chartType === "heikin_ashi") this.ha[i] = haStep(this.ha[i - 1], bar);
    this.main.update(this.point(i, ct));
    this.volume.update(this.volumeData(bar, ct));
    this.updateRealLine();
    if (!this.hovering) this.renderLegend();
    this.placeQuotes();
    this.onBars("live");
  }

  applyTick(bid: number, ask: number): void {
    if (!this.ctx) return;
    this.lastTick = { bid, ask };
    if (!this.bidLine || !this.askLine) {
      // Lines only; their labels are drawn by placeQuotes() so they never overlap.
      const common = { lineWidth: 1 as const, lineStyle: LineStyle.Dotted, axisLabelVisible: false };
      this.askLine = this.main.createPriceLine({ ...common, price: ask, color: this.palette.ask });
      this.bidLine = this.main.createPriceLine({ ...common, price: bid, color: this.palette.bid });
    } else {
      this.askLine.applyOptions({ price: ask });
      this.bidLine.applyOptions({ price: bid });
    }
    this.placeQuotes();
  }

  private removeQuoteLines(): void {
    if (this.bidLine) this.main.removePriceLine(this.bidLine);
    if (this.askLine) this.main.removePriceLine(this.askLine);
    this.bidLine = this.askLine = undefined;
  }

  // Chart type

  /**
   * Show the bars as candles, bars, a line, an area or Heikin Ashi. The price series is
   * replaced (the library cannot change a series' type); listeners move what they attached
   * to it (the drawings) to the new one.
   */
  setChartType(type: ChartType): void {
    if (type === this.chartType) return;
    const range = this.chart.timeScale().getVisibleLogicalRange();
    const tick = this.lastTick;
    this.removeQuoteLines();
    this.removeRealLine();
    const before = this.main;
    this.chartType = type;
    this.main = this.makeMain(type);
    for (const listener of this.seriesListeners) listener(before, this.main);
    this.chart.removeSeries(before);
    if (this.ctx) {
      this.applyFormat();
      this.renderSeries();
      this.renderLegend();
      if (range) this.chart.timeScale().setVisibleLogicalRange(range);
    }
    if (tick) this.applyTick(tick.bid, tick.ask);
    this.placeQuotes();
  }

  /** Called with the old and the new price series when the chart type changes. */
  onSeriesChange(listener: (before: ISeriesApi<SeriesType>, after: ISeriesApi<SeriesType>) => void): void {
    this.seriesListeners.push(listener);
  }

  private makeMain(type: ChartType): ISeriesApi<SeriesType> {
    const series =
      type === "bars"
        ? this.chart.addSeries(BarSeries)
        : type === "line"
          ? this.chart.addSeries(LineSeries)
          : type === "area"
            ? this.chart.addSeries(AreaSeries)
            : this.chart.addSeries(CandlestickSeries);
    series.applyOptions(this.mainOptions(type));
    series.setSeriesOrder(1); // above the volume, below the indicators
    return series;
  }

  private mainOptions(type: ChartType): Record<string, unknown> {
    const p = this.palette;
    const common = { priceLineStyle: LineStyle.Dotted };
    switch (type) {
      case "bars":
        return { ...common, upColor: p.up, downColor: p.down, thinBars: false };
      case "line":
        return { ...common, color: p.accent, lineWidth: 2 };
      case "area":
        return { ...common, lineColor: p.accent, lineWidth: 2, topColor: withAlpha(p.accent, 0.3), bottomColor: withAlpha(p.accent, 0.02) };
      default: {
        const candles = { ...common, upColor: p.up, downColor: p.down, wickUpColor: p.up, wickDownColor: p.down, borderVisible: false };
        // Heikin Ashi prices are not real: the real last price gets its own label.
        return type === "heikin_ashi" ? { ...candles, lastValueVisible: false, priceLineVisible: false } : candles;
      }
    }
  }

  /** The series point for bar `i`: OHLC, the close (line, area) or the Heikin Ashi bar. */
  private point(i: number, ct: number): CandlestickData | LineData {
    const time = ct as UTCTimestamp;
    if (this.chartType === "line" || this.chartType === "area") return { time, value: this.bars[i].c };
    const b = this.chartType === "heikin_ashi" ? this.ha[i] : this.bars[i];
    return { time, open: b.o, high: b.h, low: b.l, close: b.c };
  }

  private updateRealLine(): void {
    const last = this.bars[this.bars.length - 1];
    if (this.chartType !== "heikin_ashi" || !last) return this.removeRealLine();
    const color = last.c >= last.o ? this.palette.up : this.palette.down;
    const options = { price: last.c, color, lineWidth: 1 as const, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: "" };
    if (this.realLine) this.realLine.applyOptions(options);
    else this.realLine = this.main.createPriceLine(options);
  }

  private removeRealLine(): void {
    if (this.realLine) this.main.removePriceLine(this.realLine);
    this.realLine = undefined;
  }

  // View

  resetView(): void {
    this.chart.timeScale().resetTimeScale();
    this.chart.timeScale().scrollToRealTime();
    this.chart.priceScale("right").applyOptions({ autoScale: true });
  }

  setTheme(): void {
    this.palette = readPalette();
    const p = this.palette;
    this.chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: p.chart }, textColor: p.muted, panes: { separatorColor: p.line } },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      crosshair: {
        vertLine: { color: p.crosshair, labelBackgroundColor: p.raised },
        horzLine: { color: p.crosshair, labelBackgroundColor: p.raised },
      },
    });
    this.main.applyOptions(this.mainOptions(this.chartType));
    this.updateRealLine();
    this.askLine?.applyOptions({ color: p.ask });
    this.bidLine?.applyOptions({ color: p.bid });
    if (this.ctx) this.volume.setData(this.bars.map((b, i) => this.volumeData(b, this.times[i])));
  }

  // Access for the drawing layer

  /** The price series (it changes with the chart type: see onSeriesChange). */
  get priceSeries(): ISeriesApi<SeriesType> {
    return this.main;
  }

  /** Chart time of the bar that starts at server time `t`, if we hold it. */
  chartTimeOf(t: number): number | undefined {
    let lo = 0;
    let hi = this.bars.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const x = this.bars[mid].t;
      if (x === t) return this.times[mid];
      if (x < t) lo = mid + 1;
      else hi = mid - 1;
    }
    return undefined;
  }

  /** Server times of the first and last bars held (and the one before the last). */
  span(): { first: number; last: number; count: number; beforeLast?: number } | undefined {
    const n = this.bars.length;
    if (!n) return undefined;
    return { first: this.bars[0].t, last: this.bars[n - 1].t, count: n, beforeLast: this.bars[n - 2]?.t };
  }

  private indexCache?: { key: string; clock?: Clock; index: TimeIndex };

  /**
   * Clock time <-> bar index over the bars on screen (cached until they or the clock
   * change). With the instrument's trading clock, future slots skip closed periods.
   */
  timeIndex(clock?: Clock): TimeIndex {
    const n = this.bars.length;
    const key = `${n}:${this.bars[0]?.t}:${this.bars[n - 1]?.t}:${this.ctx?.timeframe.name}`;
    if (this.indexCache?.key !== key || this.indexCache.clock !== clock) {
      const tf = this.ctx?.timeframe;
      const step = tf?.seconds ?? 60;
      // 4h, 1D, 1W bars are aligned on the session's wall clock (17:00 New York).
      const advance = tf?.kind === "session" && this.ctx ? wallAdvance(step, this.ctx.session.timezone) : undefined;
      const index = new TimeIndex(this.bars.map((b) => b.t), step, clock, advance);
      this.indexCache = { key, clock, index };
    }
    return this.indexCache.index;
  }

  /** Close of the newest bar (the current price). */
  lastClose(): number | undefined {
    return this.bars[this.bars.length - 1]?.c;
  }

  barAt(index: number): Bar | undefined {
    return Number.isInteger(index) ? this.bars[index] : undefined;
  }

  formatPriceValue(x: number): string {
    return this.formatPrice(x);
  }

  /** Crosshair-style label for clock time `t` in the display timezone. */
  labelForTime(t: number): string {
    return this.ctx ? formatChartTime(this.toChart(t), this.ctx.timeframe.name) : "";
  }

  get chartBackground(): string {
    return this.palette.chart;
  }

  /** Server times of the bars on screen (for debugging and tests). */
  barTimes(): number[] {
    return this.bars.map((b) => b.t);
  }

  screenshot(): HTMLCanvasElement {
    return this.chart.takeScreenshot();
  }

  // Helpers

  private toChart(t: number): number {
    const ctx = this.ctx!;
    return toChartTimes([{ t }], ctx.timeframe.name, ctx.tz, ctx.session).times[0];
  }

  private applyFormat(): void {
    const d = this.ctx!.instrument.decimals;
    const nf = new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
    this.formatPrice = (x) => nf.format(x);
    this.main.applyOptions({ priceFormat: { type: "price", precision: d, minMove: 10 ** -d } });
    this.chart.applyOptions({ localization: { priceFormatter: this.formatPrice } });
  }

  private volumeData(b: Bar, ct: number): HistogramData {
    const color = b.c >= b.o ? this.palette.up : this.palette.down;
    return { time: ct as UTCTimestamp, value: b.v, color: withAlpha(color, 0.22) };
  }

  private showMessage(text: string | null): void {
    this.message.hidden = !text;
    this.message.textContent = text ?? "";
  }

  // Legend

  private onCrosshair(p: MouseEventParams): void {
    if (p.time === undefined || !p.point) {
      this.hovering = false;
      this.renderLegend();
      this.onHover(undefined);
      return;
    }
    this.hovering = true;
    const i = this.indexOfChartTime(p.time as number);
    this.renderLegend(i);
    this.onHover(this.bars[i]?.t);
  }

  private indexOfChartTime(ct: number): number {
    let lo = 0;
    let hi = this.times.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.times[mid] === ct) return mid;
      if (this.times[mid] < ct) lo = mid + 1;
      else hi = mid - 1;
    }
    return -1;
  }

  renderLegend(index = this.bars.length - 1): void {
    const ctx = this.ctx;
    if (!ctx) return;
    const source = ctx.instrument.source.toUpperCase();
    this.legendTitle.replaceChildren(
      h("span", { class: this.feedDotClass }),
      h("span", {}, ctx.instrument.display_name || ctx.instrument.instrument),
      h(
        "span",
        { class: "meta" },
        `${ctx.instrument.instrument} · ${ctx.timeframe.name} · ${source}${ctx.price === "mid" ? "" : " · " + ctx.price}` +
          (this.chartType === "heikin_ashi" ? " · Heikin Ashi" : ""),
      ),
    );
    const bar = this.bars[index];
    if (!bar) {
      this.legendValues.replaceChildren();
      return;
    }
    const prev = this.bars[index - 1];
    const fmt = this.formatPrice;
    const dir = bar.c >= bar.o ? "up" : "down";
    const parts: Node[] = [];
    for (const [k, v] of [["O", bar.o], ["H", bar.h], ["L", bar.l], ["C", bar.c]] as const) {
      parts.push(h("span", { class: dir }, k, h("b", {}, fmt(v))));
    }
    if (prev) {
      const change = bar.c - prev.c;
      const pct = (change / prev.c) * 100;
      const cls = change >= 0 ? "up" : "down";
      const sign = change >= 0 ? "+" : "";
      parts.push(h("span", { class: cls }, h("b", {}, `${sign}${fmt(change)} (${sign}${pct.toFixed(2)}%)`)));
    }
    parts.push(h("span", { title: "Number of price updates, not traded volume" }, "Tick vol", h("b", {}, String(bar.v))));
    this.legendValues.replaceChildren(...parts);
  }

  // Quote stack on the price scale, anchored to the last-price label:
  // Ask above it, then the bar-close countdown and Bid below it.

  private placeQuotes(): void {
    const ctx = this.ctx;
    const last = this.bars[this.bars.length - 1];
    const y = last ? this.main.priceToCoordinate(last.c) : null;
    const hideAll = () => {
      this.countdown.hidden = this.askTag.hidden = this.bidTag.hidden = true;
    };
    if (!ctx || !last || y === null) return hideAll();
    const LABEL = 18;
    const width = this.chart.priceScale("right").width();
    const place = (el: HTMLElement, top: number) => {
      el.style.top = `${top}px`;
      el.style.width = `${width}px`;
      el.hidden = false;
    };
    let below = y + LABEL / 2 + 1;

    const left = last.t + ctx.timeframe.seconds - Date.now() / 1000;
    const running = last.complete === false || this.lastTick !== undefined;
    if (running && left > 0 && left <= ctx.timeframe.seconds) {
      this.countdown.textContent = formatCountdown(left);
      this.countdown.style.background = last.c >= last.o ? this.palette.up : this.palette.down;
      place(this.countdown, below);
      below += LABEL;
    } else {
      this.countdown.hidden = true;
    }

    if (this.lastTick) {
      this.askTag.lastElementChild!.textContent = this.formatPrice(this.lastTick.ask);
      this.bidTag.lastElementChild!.textContent = this.formatPrice(this.lastTick.bid);
      place(this.askTag, y - LABEL / 2 - 1 - LABEL);
      place(this.bidTag, below);
    } else {
      this.askTag.hidden = this.bidTag.hidden = true;
    }
  }
}
