/**
 * Drawing tools: placing, selecting, dragging, deleting, and syncing with the server.
 *
 * Interaction (matches the reference recording):
 * - pick a tool, click point 1, move (the line follows), click point 2; pressing,
 *   dragging and releasing works too. A horizontal line needs one click. The tool
 *   then returns to the cursor. Escape cancels.
 * - hover shows the handles; click selects; drag a handle to move that point, drag the
 *   line to move it whole; Delete removes; right-click opens the drawing's menu.
 * - holding Ctrl while a drawing tool is active snaps to the nearest open, high, low or
 *   close of the bar under the mouse (of the price type shown), with a marker.
 *
 * Drawings are stored on the server; every change comes back over the websocket to all
 * open browsers, and `applyRemote` folds it in.
 */

import type { Logical } from "lightweight-charts";

import { ApiError, drawingsApi, type Alert, type Drawing, type DrawingPatch, type DrawingStyle } from "../api";
import { lineAlertState, pointAlong, type LineAlertState } from "../ui/alertState";
import type { ChartView } from "../chart/view";
import { closeMenu, h, menuItem, showMenuAt } from "../ui/dom";
import {
  POINTS_NEEDED,
  linePath,
  logicalX,
  nearestOhlc,
  segmentDistance,
  type DrawingType,
  type LineType,
  type Point,
} from "./geometry";
import {
  channelLines,
  contrastColor,
  dragHandle,
  fibLevels,
  fillOf,
  formatDuration,
  isLineType,
  measureStats,
  moveShape,
  rectCorners,
  shapeError,
  textSize,
  withAlpha,
} from "./shapes";
import { DrawingLayer, type MediaTarget } from "./layer";
import { tradingClock, type Clock, type Closure, type TradingHoursJson } from "./tradingTime";
import { StyleToolbar } from "./toolbar";

export type Tool = "cursor" | DrawingType;
/** A handle number (the anchors; a rectangle's four corners) or the whole drawing. */
type Part = "body" | number;

interface Hit {
  id: string;
  part: Part;
}

interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface Shape {
  path: [number, number][]; // main screen polyline (hline: two points across the pane); the bell sits on it
  segments: [number, number][][]; // everything a click can hit (path included)
  areas: Box[]; // boxes a click inside hits (text, filled rectangle, measure's info box)
  handles: ([number, number] | null)[]; // by handle number; null where the chart cannot place it
  bell?: [number, number]; // centre of the alert marker, when the line has alerts
}

/** Where an alert action was asked for: next to a button, or at a point on screen. */
export interface AlertPlace {
  anchor?: HTMLElement;
  at?: { x: number; y: number };
}

const HANDLE_RADIUS = 5;
const HIT_LINE = 6;
const HIT_HANDLE = 8;
const DRAG_THRESHOLD = 4;
const BELL_SIZE = 18;
const BELL_GAP = 14; // marker centre above the line
// Lucide "bell", drawn on the canvas at half size.
const BELL_PATHS = [
  "M10.268 21a2 2 0 0 0 3.464 0",
  "M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326",
];

export const TYPE_NAMES: Record<DrawingType, string> = {
  hline: "horizontal line",
  trendline: "trendline",
  ray: "ray",
  extended: "extended line",
  vline: "vertical line",
  rect: "rectangle",
  channel: "parallel channel",
  fib: "Fibonacci retracement",
  text: "text",
  measure: "measure",
};

function dashPattern(style: DrawingStyle): number[] {
  if (style.dash === "dashed") return [style.width * 4, style.width * 3];
  return style.dash === "dotted" ? [0.1, style.width * 2.5] : [];
}

export class DrawingController {
  tool: Tool = "cursor";
  hidden: boolean;
  selectedId?: string;
  onToolChange: (tool: Tool) => void = () => {};
  onStyleUsed: (style: DrawingStyle) => void = () => {};
  onHiddenChange: (hidden: boolean) => void = () => {};
  /** The line's bell, menu entry or Alt+A: edit its alert(s), or add one when it has none. */
  onAddAlert: (drawing: Drawing, place?: AlertPlace) => void = () => {};
  /** The alerts attached to a drawing (the line shows their state). */
  alertsOf: (drawingId: string) => Alert[] = () => [];
  onError: (text: string) => void = (text) => console.warn(text);

  private drawings = new Map<string, Drawing>();
  private instrument?: string;
  /** Slopes are measured on this instrument's trading clock (see geometry.ts). */
  private clock: Clock = (t) => t;
  private hours: TradingHoursJson | null = null;
  private hover?: Hit;
  private placing?: { type: DrawingType; points: Point[]; cursor?: Point; down?: { x: number; y: number } };
  private drag?: {
    id: string;
    part: Part;
    startLogical: number;
    startPrice: number;
    original: Point[];
    moved: boolean;
    down: { x: number; y: number };
  };
  private snap?: { logical: number; price: number };
  private ctrl = false;
  private mouse?: { x: number; y: number };
  private consume = false;
  private shapes = new Map<string, Shape>();
  private layer: DrawingLayer;
  private toolbar: StyleToolbar;

  constructor(
    private view: ChartView,
    private host: HTMLElement,
    area: HTMLElement,
    private style: () => DrawingStyle,
    hidden: boolean,
  ) {
    this.hidden = hidden;
    this.layer = new DrawingLayer(this);
    view.priceSeries.attachPrimitive(this.layer);
    // A new chart type replaces the price series: the drawings move to the new one.
    view.onSeriesChange((before, after) => {
      before.detachPrimitive(this.layer);
      after.attachPrimitive(this.layer);
    });
    this.toolbar = new StyleToolbar(area, this);

    const opts = { capture: true };
    host.addEventListener("pointerdown", (e) => this.onDown(e), opts);
    // The chart library pans on mousedown/touchstart: stop those when we use the press.
    const block = (e: Event) => {
      if (this.consume) {
        e.stopPropagation();
        e.preventDefault();
      }
    };
    host.addEventListener("mousedown", block, opts);
    host.addEventListener("touchstart", block, { capture: true, passive: false });
    host.addEventListener("pointermove", (e) => this.onMove(e), opts);
    window.addEventListener("pointermove", (e) => this.drag && this.onMove(e));
    window.addEventListener("pointerup", (e) => this.onUp(e));
    host.addEventListener("contextmenu", (e) => this.onContextMenu(e), opts);
    host.addEventListener("pointerleave", () => {
      if (!this.drag) this.setHover(undefined);
    });
    document.addEventListener("keydown", (e) => this.onKey(e, true));
    document.addEventListener("keyup", (e) => this.onKey(e, false));
    window.addEventListener("blur", () => this.setCtrl(false));
  }

  // Data

  async setInstrument(instrument: string, hours: TradingHoursJson | null, closures: readonly Closure[] = []): Promise<void> {
    this.hours = hours;
    this.clock = tradingClock(hours, closures);
    if (instrument !== this.instrument) {
      this.instrument = instrument;
      this.cancelPlacing();
      this.select(undefined);
      this.drawings.clear();
      this.redraw();
    }
    await this.reload();
  }

  /** The server learned new closures (a holiday passed): lines skip them from now on. */
  setClosures(instrument: string, closures: readonly Closure[]): void {
    if (instrument !== this.instrument) return;
    this.clock = tradingClock(this.hours, closures);
    this.redraw();
  }

  /** Bar index of the chart, with future slots placed by this instrument's clock. */
  private index() {
    return this.view.timeIndex(this.clock);
  }

  /** Fetch the drawings again (after switching instrument or reconnecting). */
  async reload(): Promise<void> {
    const instrument = this.instrument;
    if (!instrument) return;
    try {
      const list = await drawingsApi.list(instrument);
      if (instrument !== this.instrument) return;
      this.drawings = new Map(list.map((d) => [d.id, d]));
      if (this.selectedId && !this.drawings.has(this.selectedId)) this.select(undefined);
      this.redraw();
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) this.onError(`Could not load drawings: ${(err as Error).message}`);
    }
  }

  /** A change made anywhere (this browser or another), pushed by the server. */
  applyRemote(op: "create" | "update" | "delete", d: Drawing): void {
    if (d.instrument !== this.instrument) return;
    if (this.drag?.id === d.id) return; // our own pending move wins
    if (op === "delete") {
      this.drawings.delete(d.id);
      if (this.selectedId === d.id) this.select(undefined);
    } else {
      this.drawings.set(d.id, d);
      if (this.selectedId === d.id) this.toolbar.show(d);
    }
    this.redraw();
  }

  /** The alerts changed: refresh the bells. */
  alertsChanged(): void {
    const d = this.selectedId ? this.drawings.get(this.selectedId) : undefined;
    if (d) this.toolbar.show(d);
    this.redraw();
  }

  alertState(drawingId: string): LineAlertState | null {
    return lineAlertState(this.alertsOf(drawingId));
  }

  get(id: string): Drawing | undefined {
    return this.drawings.get(id);
  }

  count(): number {
    return this.drawings.size;
  }

  // Tools and selection

  setTool(tool: Tool): void {
    this.cancelPlacing();
    this.tool = tool;
    if (tool !== "cursor") {
      this.select(undefined);
      if (this.hidden) this.setHidden(false);
    }
    this.onToolChange(tool);
    this.redraw();
  }

  setHidden(hidden: boolean): void {
    this.hidden = hidden;
    if (hidden) this.select(undefined);
    this.onHiddenChange(hidden);
    this.redraw();
  }

  select(id: string | undefined): void {
    this.selectedId = id;
    const d = id ? this.drawings.get(id) : undefined;
    if (d) this.toolbar.show(d);
    else this.toolbar.hide();
    this.redraw();
  }

  private cancelPlacing(): void {
    this.placing = undefined;
    this.snap = undefined;
  }

  private setHover(hit: Hit | undefined): void {
    if (hit?.id !== this.hover?.id || hit?.part !== this.hover?.part) {
      this.hover = hit;
      this.redraw();
    }
  }

  private setCtrl(down: boolean): void {
    if (down === this.ctrl) return;
    this.ctrl = down;
    if (this.placing && this.mouse) {
      this.placing.cursor = this.pointAt(this.mouse.x, this.mouse.y) ?? this.placing.cursor;
      this.redraw();
    }
  }

  redraw(): void {
    this.layer.requestUpdate();
  }

  // Server changes

  async create(type: DrawingType, points: Point[], style = this.style(), label: string | null = null): Promise<void> {
    if (!this.instrument) return;
    try {
      const d = await drawingsApi.create({ instrument: this.instrument, type, points, style, label });
      this.drawings.set(d.id, d);
      this.select(d.id);
    } catch (err) {
      this.onError(`Could not save the drawing: ${(err as Error).message}`);
    }
  }

  async update(id: string, patch: DrawingPatch): Promise<void> {
    const before = this.drawings.get(id);
    if (!before) return;
    // Show the change at once; the server's answer (and broadcast) confirms it.
    const optimistic: Drawing = {
      ...before,
      points: patch.points ?? before.points,
      style: { ...before.style, ...patch.style },
      label: patch.clear_label ? null : (patch.label ?? before.label),
    };
    this.drawings.set(id, optimistic);
    if (this.selectedId === id) this.toolbar.show(optimistic);
    this.redraw();
    // Only a line's colour, width and dash are remembered for the next drawing (not a text's size, a fill or an extension).
    if (patch.style && before.type !== "text") {
      const { color, width, dash } = optimistic.style;
      this.onStyleUsed({ color, width, dash });
    }
    try {
      const d = await drawingsApi.update(id, patch);
      this.drawings.set(id, d);
      if (this.selectedId === id) this.toolbar.show(d);
    } catch (err) {
      this.drawings.set(id, before);
      this.onError(`Could not save the change: ${(err as Error).message}`);
    }
    this.redraw();
  }

  async remove(id: string): Promise<void> {
    const before = this.drawings.get(id);
    if (!before) return;
    this.drawings.delete(id);
    if (this.selectedId === id) this.select(undefined);
    this.redraw();
    try {
      await drawingsApi.remove(id);
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 404)) {
        this.drawings.set(id, before);
        this.redraw();
        this.onError(`Could not delete the drawing: ${(err as Error).message}`);
      }
    }
  }

  async removeAll(): Promise<void> {
    if (!this.instrument) return;
    this.drawings.clear();
    this.select(undefined);
    try {
      await drawingsApi.removeAll(this.instrument);
    } catch (err) {
      this.onError(`Could not delete the drawings: ${(err as Error).message}`);
      await this.reload();
    }
  }

  async clone(id: string): Promise<void> {
    const d = this.drawings.get(id);
    if (d) await this.create(d.type, d.points, d.style, d.label);
  }

  // Coordinates

  private paneSize(): { width: number; height: number } {
    const ts = this.view.chart.timeScale();
    return { width: ts.width(), height: this.host.clientHeight - ts.height() };
  }

  private local(e: MouseEvent): { x: number; y: number; inPane: boolean } {
    const r = this.host.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    const pane = this.paneSize();
    return { x, y, inPane: x >= 0 && y >= 0 && x < pane.width && y < pane.height };
  }

  private xOf(logical: number): number | null {
    const scale = this.view.chart.timeScale();
    return logicalX(logical, (i) => scale.logicalToCoordinate(i as Logical));
  }

  private yOf(price: number): number | null {
    return this.view.priceSeries.priceToCoordinate(price);
  }

  private round(price: number): number {
    const d = this.view.ctx?.instrument.decimals ?? 5;
    return Number(price.toFixed(d));
  }

  /** Anchor under the mouse: the nearest bar's time, the mouse price (or Ctrl snap). */
  private pointAt(x: number, y: number): Point | null {
    const index = this.index();
    const logical = this.view.chart.timeScale().coordinateToLogical(x);
    const price = this.view.priceSeries.coordinateToPrice(y);
    if (logical === null || price === null || index.size === 0) return null;
    const bar = Math.round(logical);
    let p = price as number;
    this.snap = undefined;
    if (this.ctrl && this.tool !== "cursor") {
      const b = this.view.barAt(bar);
      if (b) {
        p = nearestOhlc(b, p);
        this.snap = { logical: bar, price: p };
      }
    }
    return { t: index.toTime(bar), p: this.snap ? p : this.round(p) };
  }

  // Hit testing (on the shapes from the last paint)

  hitTest(x: number, y: number): Hit | undefined {
    if (this.hidden) return undefined;
    const ids = [...this.drawings.keys()].reverse(); // last drawn is on top
    if (this.selectedId) ids.unshift(this.selectedId);
    for (const id of ids) {
      const shape = this.shapes.get(id);
      if (!shape) continue;
      const handle = shape.handles.findIndex((hd) => hd !== null && Math.hypot(hd[0] - x, hd[1] - y) <= HIT_HANDLE);
      if (handle >= 0 && (id === this.selectedId || id === this.hover?.id)) return { id, part: handle };
      for (const path of shape.segments) {
        for (let i = 1; i < path.length; i++) {
          const [ax, ay] = path[i - 1];
          const [bx, by] = path[i];
          if (Math.max(ax, bx) < x - HIT_LINE || Math.min(ax, bx) > x + HIT_LINE) continue;
          if (segmentDistance(x, y, ax, ay, bx, by) <= HIT_LINE) return { id, part: "body" };
        }
      }
      if (shape.areas.some((a) => x >= a.x0 && x <= a.x1 && y >= a.y0 && y <= a.y1)) return { id, part: "body" };
    }
    return undefined;
  }

  /** The drawing whose alert marker is at (x, y). */
  bellAt(x: number, y: number): string | undefined {
    if (this.hidden || this.tool !== "cursor") return undefined;
    for (const [id, shape] of this.shapes) {
      if (shape.bell && Math.abs(shape.bell[0] - x) <= BELL_SIZE / 2 && Math.abs(shape.bell[1] - y) <= BELL_SIZE / 2) return id;
    }
    return undefined;
  }

  // Pointer handling

  private onDown(e: PointerEvent): void {
    this.consume = false;
    if (e.button !== 0) return;
    const pos = this.local(e);
    if (!pos.inPane) return;
    this.setCtrl(e.ctrlKey);

    if (this.tool !== "cursor") {
      this.consume = true;
      e.stopPropagation();
      e.preventDefault();
      const point = this.pointAt(pos.x, pos.y);
      if (!point) return;
      const type = this.tool;
      if (type === "text") {
        this.askText(point, e);
        return;
      }
      if (POINTS_NEEDED[type] === 1) {
        void this.finish(type, [point]);
        return;
      }
      if (!this.placing) {
        this.placing = { type, points: [point], cursor: point, down: pos };
      } else {
        this.addPoint(point);
      }
      this.redraw();
      return;
    }

    const bell = this.bellAt(pos.x, pos.y);
    if (bell) {
      this.consume = true;
      e.stopPropagation();
      e.preventDefault();
      this.select(bell);
      const r = this.host.getBoundingClientRect();
      this.onAddAlert(this.drawings.get(bell)!, { at: { x: r.left + pos.x, y: r.top + pos.y } });
      return;
    }
    const hit = this.hitTest(pos.x, pos.y);
    if (!hit) {
      if (this.selectedId) this.select(undefined);
      return; // let the chart pan
    }
    this.consume = true;
    e.stopPropagation();
    e.preventDefault();
    const d = this.drawings.get(hit.id)!;
    this.select(hit.id);
    const logical = this.view.chart.timeScale().coordinateToLogical(pos.x) ?? 0;
    const price = this.view.priceSeries.coordinateToPrice(pos.y) ?? 0;
    this.drag = {
      id: hit.id,
      part: hit.part,
      startLogical: logical,
      startPrice: price,
      original: d.points.map((p) => ({ ...p })),
      moved: false,
      down: pos,
    };
  }

  private onMove(e: PointerEvent): void {
    const pos = this.local(e);
    this.mouse = pos;
    this.setCtrl(e.ctrlKey);
    if (this.drag) {
      this.dragTo(pos.x, pos.y);
      return;
    }
    if (this.placing) {
      this.placing.cursor = this.pointAt(pos.x, pos.y) ?? this.placing.cursor;
      this.redraw();
      return;
    }
    if (this.tool !== "cursor") {
      // A tool is active but nothing placed yet: keep the snap marker live.
      const before = this.snap;
      this.pointAt(pos.x, pos.y);
      if (before !== this.snap) this.redraw();
      return;
    }
    this.setHover(pos.inPane ? this.hitTest(pos.x, pos.y) : undefined);
  }

  private onUp(e: PointerEvent): void {
    this.consume = false;
    if (this.placing?.down && this.placing.points.length === 1) {
      // Press-drag-release places both points in one gesture.
      const pos = this.local(e);
      if (Math.hypot(pos.x - this.placing.down.x, pos.y - this.placing.down.y) > DRAG_THRESHOLD * 2) {
        const point = this.pointAt(pos.x, pos.y);
        if (point) this.addPoint(point);
      } else {
        this.placing.down = undefined;
      }
    }
    if (!this.drag) return;
    const { id, moved } = this.drag;
    const d = this.drawings.get(id);
    this.drag = undefined;
    if (moved && d) void this.update(id, { points: d.points });
  }

  /**
   * The next anchor of the drawing being placed. A point that would break the shape's
   * rules (no trading time between two points, a channel's third point on its base line)
   * is ignored, so the drawing is always one the server accepts.
   */
  private addPoint(point: Point): void {
    const placing = this.placing!;
    const points = [...placing.points, point];
    const need = POINTS_NEEDED[placing.type];
    if (points.length === need ? shapeError(placing.type, points, this.clock, "x") : points.length === 2 && this.clock(points[0].t) === this.clock(points[1].t)) return;
    placing.points = points;
    placing.down = undefined;
    if (points.length === need) void this.finish(placing.type, points);
  }

  private async finish(type: DrawingType, points: Point[], label: string | null = null): Promise<void> {
    this.placing = undefined;
    this.snap = undefined;
    this.setTool("cursor");
    await this.create(type, points, this.style(), label);
  }

  /** A text is placed with one click, then its words are asked for next to the click. */
  private askText(point: Point, e: PointerEvent): void {
    this.cancelPlacing();
    const input = h("input", { class: "input", maxlength: "100", placeholder: "Text" });
    const save = () => {
      const text = input.value.trim();
      if (!text) return;
      closeMenu();
      void this.finish("text", [point], text);
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") save();
      if (ev.key === "Escape") closeMenu();
    });
    const ok = h("button", { class: "btn btn-primary", type: "button" }, "Add");
    ok.addEventListener("click", save);
    showMenuAt(e.clientX, e.clientY, h("div", { class: "label-editor" }, input, ok));
    input.focus();
  }

  private dragTo(x: number, y: number): void {
    const drag = this.drag!;
    const d = this.drawings.get(drag.id);
    if (!d) return;
    if (!drag.moved && Math.hypot(x - drag.down.x, y - drag.down.y) < DRAG_THRESHOLD) return;
    drag.moved = true;
    const index = this.index();
    const logical = this.view.chart.timeScale().coordinateToLogical(x);
    const price = this.view.priceSeries.coordinateToPrice(y);
    if (logical === null || price === null) return;
    let points: Point[] | null;
    if (drag.part === "body") {
      const dl = Math.round(logical - drag.startLogical);
      const dp = (price as number) - drag.startPrice;
      points = moveShape(d.type, drag.original, dl, dp, index, (p) => this.round(p));
    } else {
      const to = { t: index.toTime(Math.round(logical)), p: this.round(price as number) };
      points = dragHandle(d.type, drag.original, drag.part, to, this.clock);
    }
    if (!points) return;
    this.drawings.set(d.id, { ...d, points });
    this.redraw();
  }

  private onContextMenu(e: MouseEvent): void {
    const pos = this.local(e);
    if (this.placing) {
      e.preventDefault();
      this.setTool("cursor");
      return;
    }
    const hit = pos.inPane ? this.hitTest(pos.x, pos.y) : undefined;
    if (!hit) return;
    e.preventDefault();
    e.stopPropagation();
    this.select(hit.id);
    const d = this.drawings.get(hit.id)!;
    const name = TYPE_NAMES[d.type];
    const alerts = this.alertsOf(d.id);
    const at = { x: e.clientX, y: e.clientY };
    const labelled = d.type !== "fib" && d.type !== "measure";
    const alertItems = !isLineType(d.type)
      ? []
      : alerts.length
      ? [
          menuItem({
            label: alerts.length === 1 ? "Edit alert…" : `Edit alert… (${alerts.length})`,
            icon: this.alertState(d.id) === "active" ? "bellRing" : "bellOff",
            hint: "Alt+A",
            onClick: () => this.onAddAlert(d, { at }),
          }),
        ]
      : [menuItem({ label: `Add alert on ${name}…`, icon: "bell", hint: "Alt+A", onClick: () => this.onAddAlert(d, { at }) })];
    showMenuAt(
      e.clientX,
      e.clientY,
      h(
        "div",
        { style: "min-width:230px" },
        ...alertItems,
        ...(alertItems.length ? [h("hr")] : []),
        menuItem({ label: "Edit style…", icon: "palette", onClick: () => this.toolbar.openColor() }),
        ...(labelled
          ? [
              menuItem({
                label: d.type === "text" ? "Edit text…" : d.label ? "Edit label…" : "Add label…",
                icon: "text",
                onClick: () => this.toolbar.openLabel(),
              }),
            ]
          : []),
        menuItem({ label: "Clone", icon: "copy", onClick: () => void this.clone(d.id) }),
        h("hr"),
        menuItem({ label: "Delete", icon: "trash", hint: "Del", onClick: () => void this.remove(d.id) }),
      ),
    );
  }

  private onKey(e: KeyboardEvent, down: boolean): void {
    if (e.key === "Control") {
      this.setCtrl(down);
      return;
    }
    if (!down) return;
    const target = e.target;
    if (target instanceof Element && target.closest("input, select, textarea, [contenteditable]") || document.querySelector(".modal-backdrop, .auth-screen")) return;
    if (e.key === "Escape") {
      if (this.placing || this.tool !== "cursor") this.setTool("cursor");
      else if (this.selectedId && !document.querySelector(".menu")) this.select(undefined);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && this.selectedId) {
      e.preventDefault();
      void this.remove(this.selectedId);
      return;
    }
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const keys: Record<string, Tool> = {
        t: "trendline",
        h: "hline",
        y: "ray",
        e: "extended",
        v: "vline",
        b: "rect",
        c: "channel",
        f: "fib",
        x: "text",
        m: "measure",
      };
      const tool = keys[e.key.toLowerCase()];
      if (tool) {
        e.preventDefault();
        this.setTool(this.tool === tool ? "cursor" : tool);
      }
    }
  }

  // Painting (called by the layer on every chart repaint)

  draw(target: MediaTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      this.shapes.clear();
      const range = this.view.chart.timeScale().getVisibleLogicalRange();
      if (!range) return;
      const from = range.from - 2;
      const to = range.to + 2;
      if (!this.hidden) {
        for (const d of this.drawings.values()) {
          const emphasis = d.id === this.selectedId || d.id === this.hover?.id || d.id === this.drag?.id;
          const shape = this.paint(ctx, mediaSize, d.type, d.points, d.style, emphasis, from, to, d.label);
          if (!shape) continue;
          const state = isLineType(d.type) ? this.alertState(d.id) : null;
          if (state && shape.path.length > 1) {
            const at = pointAlong(shape.path, d.type === "hline" ? 0.75 : 0.5);
            if (at && at[0] > 0 && at[0] < mediaSize.width) {
              shape.bell = [at[0], at[1] - BELL_GAP];
              this.paintBell(ctx, shape.bell, d.style.color, state);
            }
          }
          this.shapes.set(d.id, shape);
        }
      }
      const p = this.placing;
      if (p?.cursor) {
        const pts = [...p.points, p.cursor];
        const style = this.style();
        const full = pts.length === POINTS_NEEDED[p.type];
        if (p.type === "channel" && pts.length === 2) {
          this.paintPreview(ctx, mediaSize, "trendline", pts, style, from, to);
        } else if (p.type === "channel" && full && shapeError("channel", pts, this.clock)) {
          // The pointer is on the base line: show the base alone.
          this.paintPreview(ctx, mediaSize, "trendline", pts.slice(0, 2), style, from, to);
        } else {
          this.paintPreview(ctx, mediaSize, p.type, pts, style, from, to);
        }
      }
      if (this.snap) {
        const x = this.xOf(this.snap.logical);
        const y = this.yOf(this.snap.price);
        if (x !== null && y !== null) {
          ctx.save();
          ctx.fillStyle = this.style().color;
          ctx.strokeStyle = this.view.chartBackground;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(x, y, 4, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
          ctx.restore();
        }
      }
    });
  }

  private applyStroke(ctx: CanvasRenderingContext2D, style: DrawingStyle): void {
    ctx.strokeStyle = style.color;
    ctx.lineWidth = style.width;
    ctx.lineCap = style.dash === "dotted" ? "round" : "butt";
    ctx.setLineDash(dashPattern(style));
  }

  private strokePath(ctx: CanvasRenderingContext2D, path: [number, number][]): void {
    if (path.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(path[0][0], path[0][1]);
    for (const [x, y] of path.slice(1)) ctx.lineTo(x, y);
    ctx.stroke();
  }

  /** Screen position of an anchor, or null where the chart cannot place it. */
  private anchorXY(p: Point): [number, number] | null {
    const x = this.xOf(this.index().toLogical(p.t));
    const y = this.yOf(p.p);
    return x !== null && y !== null && Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null;
  }

  private paint(
    ctx: CanvasRenderingContext2D,
    size: { width: number; height: number },
    type: DrawingType,
    pts: Point[],
    style: DrawingStyle,
    emphasis: boolean,
    from: number,
    to: number,
    label: string | null,
  ): Shape | undefined {
    ctx.save();
    let shape: Shape | undefined;
    if (isLineType(type)) shape = this.paintLine(ctx, size.width, type, pts, style, from, to, label);
    else if (type === "vline") shape = this.paintVline(ctx, size.height, pts, style, label);
    else if (type === "rect") shape = this.paintRect(ctx, pts, style, label);
    else if (type === "channel") shape = this.paintChannel(ctx, size.width, pts, style, from, to, label);
    else if (type === "fib") shape = this.paintFib(ctx, size.width, pts, style);
    else if (type === "text") shape = this.paintText(ctx, pts, style, label ?? "", emphasis);
    else if (type === "measure") shape = this.paintMeasure(ctx, size, pts, style);
    ctx.restore();
    if (shape && emphasis) for (const hd of shape.handles) if (hd) this.drawHandle(ctx, hd[0], hd[1], style.color);
    return shape;
  }

  private paintLine(
    ctx: CanvasRenderingContext2D,
    width: number,
    type: LineType,
    pts: Point[],
    style: DrawingStyle,
    from: number,
    to: number,
    label: string | null,
  ): Shape | undefined {
    let path: [number, number][] = [];
    if (type === "hline") {
      const y = this.yOf(pts[0].p);
      if (y === null) return undefined;
      path = [
        [0, y],
        [width, y],
      ];
    } else {
      path = linePath(type, pts, this.index(), from, to, this.clock, (l) => this.xOf(l), (p) => this.yOf(p));
    }
    this.applyStroke(ctx, style);
    this.strokePath(ctx, path);
    this.paintLineLabel(ctx, path, type === "hline", width, style, label);
    // One handle in the middle of a horizontal line's visible part, to drag it up or down.
    const handles = type === "hline" ? [[width / 2, path[0][1]] as [number, number]] : pts.map((p) => this.anchorXY(p));
    return { path, segments: [path], areas: [], handles };
  }

  private paintLineLabel(
    ctx: CanvasRenderingContext2D,
    path: [number, number][],
    flat: boolean,
    width: number,
    style: DrawingStyle,
    label: string | null,
  ): void {
    if (!label || path.length < 2) return;
    const [x, y] = flat ? [width - 8, path[0][1]] : path[path.length - 1];
    ctx.save();
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = style.color;
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText(label, Math.min(x, width - 8), y - 5);
    ctx.restore();
  }

  private paintVline(ctx: CanvasRenderingContext2D, height: number, pts: Point[], style: DrawingStyle, label: string | null): Shape | undefined {
    const x = this.timeX(pts[0].t);
    if (x === null || !Number.isFinite(x)) return undefined;
    const path: [number, number][] = [
      [x, 0],
      [x, height],
    ];
    this.applyStroke(ctx, style);
    this.strokePath(ctx, path);
    if (label) {
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillStyle = style.color;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(label, x + 5, 4);
    }
    return { path, segments: [path], areas: [], handles: [[x, height / 2]] };
  }

  private paintRect(ctx: CanvasRenderingContext2D, pts: Point[], style: DrawingStyle, label: string | null): Shape | undefined {
    const corners = rectCorners(pts).map((p) => this.anchorXY(p));
    if (corners.some((c) => c === null)) return undefined;
    const xs = corners.map((c) => c![0]);
    const ys = corners.map((c) => c![1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const fill = fillOf("rect", style.fill);
    if (fill > 0) {
      ctx.fillStyle = withAlpha(style.color, fill);
      ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    }
    this.applyStroke(ctx, style);
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    if (label) {
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillStyle = style.color;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(label, x0 + 6, y0 + 5);
    }
    const path: [number, number][] = [
      [x0, y0],
      [x1, y0],
      [x1, y1],
      [x0, y1],
      [x0, y0],
    ];
    return { path, segments: [path], areas: fill > 0 ? [{ x0, y0, x1, y1 }] : [], handles: corners };
  }

  private paintChannel(
    ctx: CanvasRenderingContext2D,
    width: number,
    pts: Point[],
    style: DrawingStyle,
    from: number,
    to: number,
    label: string | null,
  ): Shape | undefined {
    const index = this.index();
    const paths = channelLines(pts, this.clock, Boolean(style.extend)).map((l) =>
      linePath(l.type as Exclude<LineType, "hline">, l.pts, index, from, to, this.clock, (g) => this.xOf(g), (p) => this.yOf(p)),
    );
    const fill = fillOf("channel", style.fill);
    if (fill > 0 && paths[0].length > 1 && paths[1].length > 1) {
      ctx.beginPath();
      ctx.moveTo(paths[0][0][0], paths[0][0][1]);
      for (const [x, y] of paths[0].slice(1)) ctx.lineTo(x, y);
      for (const [x, y] of [...paths[1]].reverse()) ctx.lineTo(x, y);
      ctx.closePath();
      ctx.fillStyle = withAlpha(style.color, fill);
      ctx.fill();
    }
    this.applyStroke(ctx, style);
    for (const path of paths) this.strokePath(ctx, path);
    this.paintLineLabel(ctx, paths[0], false, width, style, label);
    return { path: paths[0], segments: paths, areas: [], handles: pts.map((p) => this.anchorXY(p)) };
  }

  private paintFib(ctx: CanvasRenderingContext2D, width: number, pts: Point[], style: DrawingStyle): Shape | undefined {
    const ends = pts.map((p) => this.anchorXY(p));
    const left = this.timeX(Math.min(pts[0].t, pts[1].t));
    const right = style.extend ? width : this.timeX(Math.max(pts[0].t, pts[1].t));
    if (left === null || right === null || !ends[0] || !ends[1]) return undefined;
    const segments: [number, number][][] = [];
    this.applyStroke(ctx, style);
    ctx.font = "11px system-ui, sans-serif";
    for (const { level, price } of fibLevels(pts)) {
      const y = this.yOf(price);
      if (y === null) continue;
      const seg: [number, number][] = [
        [left, y],
        [right, y],
      ];
      this.strokePath(ctx, seg);
      segments.push(seg);
      ctx.save();
      ctx.fillStyle = style.color;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(`${level} (${this.formatPrice(price)})`, Math.max(left, 0) + 4, y - 3);
      ctx.restore();
    }
    const diagonal: [number, number][] = [ends[0], ends[1]];
    ctx.save();
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    this.strokePath(ctx, diagonal);
    ctx.restore();
    segments.push(diagonal);
    return { path: diagonal, segments, areas: [], handles: ends };
  }

  private paintText(ctx: CanvasRenderingContext2D, pts: Point[], style: DrawingStyle, text: string, emphasis: boolean): Shape | undefined {
    const at = this.anchorXY(pts[0]);
    if (!at || !text) return undefined;
    const size = textSize(style.width);
    ctx.font = `${size}px system-ui, sans-serif`;
    const w = ctx.measureText(text).width;
    ctx.fillStyle = style.color;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(text, at[0], at[1]);
    const box = { x0: at[0] - 3, y0: at[1] - size - 3, x1: at[0] + w + 3, y1: at[1] + 3 };
    if (emphasis) {
      ctx.strokeStyle = style.color;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.strokeRect(box.x0, box.y0, box.x1 - box.x0, box.y1 - box.y0);
    }
    return { path: [], segments: [], areas: [box], handles: [at] };
  }

  private paintMeasure(ctx: CanvasRenderingContext2D, size: { width: number; height: number }, pts: Point[], style: DrawingStyle): Shape | undefined {
    const a = this.anchorXY(pts[0]);
    const b = this.anchorXY(pts[1]);
    if (!a || !b) return undefined;
    const inst = this.view.ctx?.instrument;
    const stats = measureStats(pts, this.index(), inst?.pip_size);
    const [x0, x1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])];
    const [y0, y1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
    ctx.fillStyle = withAlpha(style.color, 12);
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    this.applyStroke(ctx, style);
    const line: [number, number][] = [a, b];
    this.strokePath(ctx, line);
    // Arrow head at the second anchor.
    const angle = Math.atan2(b[1] - a[1], b[0] - a[0]);
    ctx.setLineDash([]);
    ctx.beginPath();
    for (const turn of [-0.45, 0.45]) {
      ctx.moveTo(b[0], b[1]);
      ctx.lineTo(b[0] - 10 * Math.cos(angle + turn), b[1] - 10 * Math.sin(angle + turn));
    }
    ctx.stroke();

    const sign = stats.change >= 0 ? "+" : "−";
    const decimals = inst?.decimals ?? 5;
    const pips = stats.pips === null ? "" : `  ${Math.abs(stats.pips).toFixed(1)} pips`;
    const lines = [
      `${sign}${Math.abs(stats.change).toFixed(decimals)} (${sign}${Math.abs(stats.percent).toFixed(2)}%)${pips}`,
      `${Math.abs(stats.bars)} ${Math.abs(stats.bars) === 1 ? "bar" : "bars"}, ${formatDuration(stats.seconds)}`,
    ];
    ctx.font = "12px system-ui, sans-serif";
    const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 14;
    const hgt = lines.length * 16 + 8;
    const bx = Math.min(Math.max((x0 + x1) / 2 - w / 2, 4), Math.max(4, size.width - w - 4));
    const by = Math.min(Math.max(stats.change >= 0 ? y0 - hgt - 8 : y1 + 8, 4), Math.max(4, size.height - hgt - 4));
    ctx.fillStyle = style.color;
    ctx.beginPath();
    ctx.roundRect(bx, by, w, hgt, 4);
    ctx.fill();
    ctx.fillStyle = contrastColor(style.color);
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    lines.forEach((l, i) => ctx.fillText(l, bx + 7, by + 4 + 8 + i * 16));
    const outline: [number, number][] = [
      [x0, y0],
      [x1, y0],
      [x1, y1],
      [x0, y1],
      [x0, y0],
    ];
    return { path: line, segments: [line, outline], areas: [{ x0: bx, y0: by, x1: bx + w, y1: by + hgt }], handles: [a, b] };
  }

  /** The drawing being placed, followed by the pointer; just the first anchor while it would be degenerate. */
  private paintPreview(
    ctx: CanvasRenderingContext2D,
    size: { width: number; height: number },
    type: DrawingType,
    pts: Point[],
    style: DrawingStyle,
    from: number,
    to: number,
  ): void {
    const full = pts.length === POINTS_NEEDED[type];
    const bad = full ? shapeError(type, pts, this.clock, "x") : pts.length === 2 && this.clock(pts[0].t) === this.clock(pts[1].t);
    if (bad) this.paintHandle(ctx, pts[0], style.color);
    else this.paint(ctx, size, type, pts, style, true, from, to, type === "text" ? "x" : null);
  }

  /** The alert marker above a line: a bell in the line's colour, struck through when no alert is active. */
  private paintBell(ctx: CanvasRenderingContext2D, [cx, cy]: [number, number], color: string, state: LineAlertState): void {
    const s = BELL_SIZE;
    const muted = "#8b93a1";
    ctx.save();
    ctx.beginPath();
    ctx.roundRect(cx - s / 2, cy - s / 2, s, s, 4);
    ctx.fillStyle = state === "active" ? color : this.view.chartBackground;
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = state === "active" ? color : muted;
    ctx.stroke();
    ctx.translate(cx - 6, cy - 6);
    ctx.scale(0.5, 0.5);
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = state === "active" ? this.view.chartBackground : muted;
    for (const d of BELL_PATHS) ctx.stroke(new Path2D(d));
    if (state === "inactive") {
      ctx.beginPath();
      ctx.moveTo(3, 3);
      ctx.lineTo(21, 21);
      ctx.stroke();
    }
    ctx.restore();
  }

  private paintHandle(ctx: CanvasRenderingContext2D, p: Point, color: string): void {
    const x = this.xOf(this.index().toLogical(p.t));
    const y = this.yOf(p.p);
    if (x !== null && y !== null) this.drawHandle(ctx, x, y, color);
  }

  private drawHandle(ctx: CanvasRenderingContext2D, x: number, y: number, color: string): void {
    ctx.save();
    ctx.fillStyle = this.view.chartBackground;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(x, y, HANDLE_RADIUS, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  /** Points whose prices and times are labelled on the axes (selected or being drawn). */
  axisPoints(): { points: Point[]; color: string; price: boolean; time: boolean } | undefined {
    if (this.placing?.cursor) {
      const points = [...this.placing.points, this.placing.cursor];
      return { points, color: this.style().color, price: true, time: points.length >= 2 };
    }
    const d = this.selectedId ? this.drawings.get(this.selectedId) : undefined;
    if (!d || this.hidden || d.type === "text") return undefined;
    return { points: d.points, color: d.style.color, price: d.type !== "vline", time: d.type !== "hline" };
  }

  priceY(p: number): number | null {
    return this.yOf(p);
  }

  timeX(t: number): number | null {
    return this.xOf(this.index().toLogical(t));
  }

  formatPrice(p: number): string {
    return this.view.formatPriceValue(p);
  }

  formatTime(t: number): string {
    return this.view.labelForTime(t);
  }

  cursorFor(x: number, y: number): string | null {
    if (this.tool !== "cursor") return null;
    if (this.bellAt(x, y)) return "pointer";
    const hit = this.hitTest(x, y);
    if (!hit) return null;
    return hit.part === "body" ? (this.drag ? "grabbing" : "pointer") : "move";
  }
}
