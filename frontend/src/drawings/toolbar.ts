/** Floating style toolbar above the chart for the selected drawing. */

import type { Drawing, DrawingStyle } from "../api";
import { lineAlertTitle } from "../ui/alertState";
import { button, closeMenu, h, icon, menuItem, showMenu } from "../ui/dom";
import type { DrawingController } from "./controller";
import { TYPE_NAMES, isLocked } from "./management";
import { fillOf, isLineType, styleOptions, textSize } from "./shapes";

export const PALETTE = [
  "#4d8dff", "#22c55e", "#ef4444", "#f59e0b", "#a855f7", "#06b6d4",
  "#ec4899", "#eab308", "#f97316", "#14b8a6", "#8b5cf6", "#84cc16",
  "#ffffff", "#cbd5e1", "#94a3b8", "#64748b", "#334155", "#000000",
];

const DASHES: [DrawingStyle["dash"], string][] = [
  ["solid", "Solid"],
  ["dashed", "Dashed"],
  ["dotted", "Dotted"],
];

function linePreview(style: Partial<DrawingStyle>): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "34");
  svg.setAttribute("height", "10");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  line.setAttribute("x1", "2");
  line.setAttribute("x2", "32");
  line.setAttribute("y1", "5");
  line.setAttribute("y2", "5");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", String(style.width ?? 2));
  line.setAttribute("stroke-linecap", style.dash === "dotted" ? "round" : "butt");
  const w = style.width ?? 2;
  if (style.dash === "dashed") line.setAttribute("stroke-dasharray", `${w * 3} ${w * 2}`);
  if (style.dash === "dotted") line.setAttribute("stroke-dasharray", `0.1 ${w * 2.5}`);
  svg.append(line);
  return svg;
}

export class StyleToolbar {
  private el: HTMLElement;
  private current?: Drawing;
  private colorBtn: HTMLButtonElement;
  private widthBtn: HTMLButtonElement;
  private dashBtn: HTMLButtonElement;
  private labelBtn: HTMLButtonElement;
  private bellBtn: HTMLButtonElement;
  private fillBtn: HTMLButtonElement;
  private extendBtn: HTMLButtonElement;
  private sepBtn: HTMLElement;
  private lockBtn: HTMLButtonElement;
  private trashBtn: HTMLButtonElement;

  constructor(
    area: HTMLElement,
    private ctl: DrawingController,
  ) {
    this.colorBtn = button({ title: "Colour", class: "dt-color", onClick: () => this.openColor() });
    this.widthBtn = button({ label: "2px", title: "Line width", onClick: () => this.openWidth() });
    this.dashBtn = button({ title: "Line style", onClick: () => this.openDash() });
    this.labelBtn = button({ icon: "text", title: "Label", onClick: () => this.openLabel() });
    this.fillBtn = button({ label: "Fill", title: "Area opacity", onClick: () => this.openFill() });
    this.extendBtn = button({ label: "Extend", onClick: () => this.toggleExtend() });
    this.sepBtn = h("div", { class: "sep" });
    this.lockBtn = button({ icon: "lock", title: "Lock", onClick: () => this.current && void this.ctl.update(this.current.id, { style: { locked: !isLocked(this.current) } }) });
    this.trashBtn = button({ icon: "trash", title: "Delete (Del)", onClick: () => this.current && void this.ctl.removeFromChart(this.current.id) });
    this.bellBtn = button({
      icon: "bell",
      title: "Add alert on this line (Alt+A)",
      class: "dt-bell",
      onClick: () => this.current && this.ctl.onAddAlert(this.current, { anchor: this.bellBtn }),
    });
    this.el = h(
      "div",
      { class: "draw-toolbar", hidden: true, role: "toolbar", "aria-label": "Drawing style" },
      this.colorBtn,
      this.widthBtn,
      this.dashBtn,
      this.labelBtn,
      this.fillBtn,
      this.extendBtn,
      this.sepBtn,
      this.bellBtn,
      button({ icon: "copy", title: "Clone", onClick: () => this.current && void this.ctl.clone(this.current.id) }),
      this.lockBtn,
      this.trashBtn,
    );
    // Clicks on the toolbar must not reach the chart.
    this.el.addEventListener("pointerdown", (e) => e.stopPropagation());
    area.append(this.el);
  }

  show(d: Drawing): void {
    this.current = d;
    this.colorBtn.replaceChildren(h("span", { class: "swatch", style: `background:${d.style.color}` }));
    const opts = styleOptions(d.type);
    this.widthBtn.querySelector("span")!.textContent = d.type === "text" ? `${textSize(d.style.width)}px` : `${d.style.width}px`;
    this.widthBtn.dataset.tip = d.type === "text" ? "Text size" : "Line width";
    this.dashBtn.replaceChildren(linePreview({ dash: d.style.dash, width: 2 }));
    this.dashBtn.hidden = !opts.dash;
    this.labelBtn.hidden = !opts.label;
    this.labelBtn.dataset.tip = d.type === "text" ? "Edit text" : "Label";
    this.labelBtn.classList.toggle("active", Boolean(d.label) && d.type !== "text");
    this.fillBtn.hidden = !opts.fill;
    this.fillBtn.querySelector("span")!.textContent = `Fill ${fillOf(d.type, d.style.fill)}%`;
    this.extendBtn.hidden = !opts.extend;
    this.extendBtn.classList.toggle("active", Boolean(d.style.extend));
    this.extendBtn.dataset.tip = d.type === "fib" ? "Extend the levels to the right edge" : "Extend both lines to the right";
    this.extendBtn.setAttribute("aria-pressed", String(Boolean(d.style.extend)));
    const locked = isLocked(d);
    this.lockBtn.replaceChildren(icon(locked ? "lockOpen" : "lock"));
    this.lockBtn.classList.toggle("active", locked);
    this.lockBtn.dataset.tip = locked ? "Unlock. Locks guard against accidents in this browser; the server does not enforce them" : "Lock. Guards against accidents in this browser: the chart will not move or delete it";
    this.trashBtn.disabled = locked;
    this.trashBtn.dataset.tip = locked ? "Unlock to delete" : "Delete (Del)";
    const alertable = isLineType(d.type);
    this.bellBtn.hidden = !alertable;
    this.sepBtn.hidden = !alertable;
    // The bell shows whether the line has alerts, and whether any of them is active.
    const state = this.ctl.alertState(d.id);
    this.bellBtn.replaceChildren(icon(state === "active" ? "bellRing" : state === "inactive" ? "bellOff" : "bell"));
    this.bellBtn.classList.toggle("alert-on", state === "active");
    this.bellBtn.classList.toggle("alert-off", state === "inactive");
    const title = lineAlertTitle(this.ctl.alertsOf(d.id));
    this.bellBtn.setAttribute("aria-label", title);
    this.bellBtn.dataset.tip = title;
    this.el.dataset.type = TYPE_NAMES[d.type];
    this.el.hidden = false;
  }

  hide(): void {
    this.current = undefined;
    this.el.hidden = true;
  }

  private toggleExtend(): void {
    if (this.current) this.set({ extend: !this.current.style.extend });
  }

  private openFill(): void {
    const d = this.current;
    if (!d) return;
    const now = fillOf(d.type, d.style.fill);
    showMenu(
      this.fillBtn,
      h(
        "div",
        {},
        ...[0, 10, 20, 30, 50].map((pct) =>
          menuItem({ label: pct === 0 ? "No fill" : `${pct}%`, selected: pct === now, onClick: () => this.set({ fill: pct }) }),
        ),
      ),
    );
  }

  private set(style: Partial<DrawingStyle>): void {
    if (this.current) void this.ctl.update(this.current.id, { style });
  }

  openColor(): void {
    const d = this.current;
    if (!d) return;
    const grid = h("div", { class: "palette" });
    for (const c of PALETTE) {
      const b = h("button", {
        type: "button",
        class: `palette-swatch${c.toLowerCase() === d.style.color.toLowerCase() ? " on" : ""}`,
        style: `background:${c}`,
        "aria-label": c,
      });
      b.addEventListener("click", () => {
        this.set({ color: c });
        closeMenu();
      });
      grid.append(b);
    }
    const custom = h("input", { type: "color", class: "palette-custom", value: d.style.color, title: "Other colour" });
    custom.addEventListener("change", () => this.set({ color: custom.value }));
    showMenu(this.colorBtn, h("div", {}, grid, h("hr"), h("label", { class: "palette-row" }, "Other colour", custom)));
  }

  private openWidth(): void {
    const d = this.current;
    if (!d) return;
    if (d.type === "text") {
      showMenu(
        this.widthBtn,
        h(
          "div",
          {},
          ...[1, 2, 3, 4].map((w) => menuItem({ label: `${textSize(w)}px`, selected: w === d.style.width, onClick: () => this.set({ width: w }) })),
        ),
      );
      return;
    }
    showMenu(
      this.widthBtn,
      h(
        "div",
        {},
        ...[1, 2, 3, 4].map((w) =>
          menuItem({ label: `${w}px`, selected: w === d.style.width, extra: h("span", { class: "hint" }, linePreview({ width: w })), onClick: () => this.set({ width: w }) }),
        ),
      ),
    );
  }

  private openDash(): void {
    const d = this.current;
    if (!d) return;
    showMenu(
      this.dashBtn,
      h(
        "div",
        {},
        ...DASHES.map(([dash, label]) =>
          menuItem({ label, selected: dash === d.style.dash, extra: h("span", { class: "hint" }, linePreview({ dash, width: 2 })), onClick: () => this.set({ dash }) }),
        ),
      ),
    );
  }

  openLabel(): void {
    const d = this.current;
    if (!d) return;
    const input = h("input", { class: "input", maxlength: "100", placeholder: d.type === "text" ? "Text" : "Text shown on the line" });
    input.value = d.label ?? "";
    const save = () => {
      const text = input.value.trim();
      if (!text && d.type === "text") return; // a text keeps its words
      void this.ctl.update(d.id, text ? { label: text } : { clear_label: true });
      closeMenu();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") save();
    });
    const ok = h("button", { class: "btn btn-primary", type: "button" }, "Save");
    ok.addEventListener("click", save);
    showMenu(this.labelBtn, h("div", { class: "label-editor" }, input, ok));
    input.focus();
    input.select();
  }
}

