/** Floating style toolbar above the chart for the selected drawing. */

import type { Drawing, DrawingStyle } from "../api";
import { button, closeMenu, h, menuItem, showMenu } from "../ui/dom";
import type { DrawingController } from "./controller";
import { TYPE_NAMES } from "./controller";

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

  constructor(
    area: HTMLElement,
    private ctl: DrawingController,
  ) {
    this.colorBtn = button({ title: "Colour", class: "dt-color", onClick: () => this.openColor() });
    this.widthBtn = button({ label: "2px", title: "Line width", onClick: () => this.openWidth() });
    this.dashBtn = button({ title: "Line style", onClick: () => this.openDash() });
    this.labelBtn = button({ icon: "text", title: "Label", onClick: () => this.openLabel() });
    this.el = h(
      "div",
      { class: "draw-toolbar", hidden: true, role: "toolbar", "aria-label": "Drawing style" },
      this.colorBtn,
      this.widthBtn,
      this.dashBtn,
      this.labelBtn,
      h("div", { class: "sep" }),
      button({ icon: "bell", title: "Add alert on this line (Alt+A)", onClick: () => this.current && this.ctl.onAddAlert(this.current) }),
      button({ icon: "copy", title: "Clone", onClick: () => this.current && void this.ctl.clone(this.current.id) }),
      button({ icon: "trash", title: "Delete (Del)", onClick: () => this.current && void this.ctl.remove(this.current.id) }),
    );
    // Clicks on the toolbar must not reach the chart.
    this.el.addEventListener("pointerdown", (e) => e.stopPropagation());
    area.append(this.el);
  }

  show(d: Drawing): void {
    this.current = d;
    this.colorBtn.replaceChildren(h("span", { class: "swatch", style: `background:${d.style.color}` }));
    this.widthBtn.querySelector("span")!.textContent = `${d.style.width}px`;
    this.dashBtn.replaceChildren(linePreview({ dash: d.style.dash, width: 2 }));
    this.labelBtn.classList.toggle("active", Boolean(d.label));
    this.el.dataset.type = TYPE_NAMES[d.type];
    this.el.hidden = false;
  }

  hide(): void {
    this.current = undefined;
    this.el.hidden = true;
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
    const input = h("input", { class: "input", maxlength: "100", placeholder: "Text shown on the line" });
    input.value = d.label ?? "";
    const save = () => {
      const text = input.value.trim();
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

