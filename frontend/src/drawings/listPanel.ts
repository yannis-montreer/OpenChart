/** The list of the instrument's drawings, with a lock, a hide and a delete button on each row. */

import type { Drawing } from "../api";
import { button, h, icon, showMenu, type IconName } from "../ui/dom";
import type { DrawingController } from "./controller";
import { isHiddenDrawing, isLocked, rowTitle } from "./management";

export class DrawingsPanel {
  private content?: HTMLElement;
  private signature = "";

  constructor(private readonly ctl: DrawingController) {
    ctl.onListChange = () => this.refresh();
  }

  open(anchor: HTMLElement): void {
    this.content = h("div", { class: "drawings-list" });
    this.signature = "";
    this.render(); // before it is shown, so the menu is placed by its real size
    showMenu(anchor, this.content, "right");
  }

  /** Draw the list again when something in it changed (the chart repaints often). */
  private refresh(): void {
    if (this.content?.isConnected) this.render();
  }

  private render(): void {
    const el = this.content;
    if (!el) return;
    const list = this.ctl.list();
    const signature = JSON.stringify([this.ctl.selectedId, list.map((d) => [d.id, d.type, d.label, d.style.locked, d.style.hidden, d.points[0]?.p])]);
    if (signature === this.signature) return;
    this.signature = signature;

    const allHidden = list.length > 0 && list.every(isHiddenDrawing);
    const allLocked = list.length > 0 && list.every(isLocked);
    const head = h(
      "div",
      { class: "dl-head" },
      h("strong", {}, `Drawings (${list.length})`),
      h("div", { class: "grow" }),
      button({ label: allHidden ? "Show all" : "Hide all", title: allHidden ? "Show every drawing" : "Hide every drawing", disabled: !list.length, onClick: () => void this.ctl.setAll("hidden", !allHidden) }),
      button({ label: allLocked ? "Unlock all" : "Lock all", title: allLocked ? "Unlock every drawing" : "Lock every drawing", disabled: !list.length, onClick: () => void this.ctl.setAll("locked", !allLocked) }),
    );
    const rows = list.length ? list.map((d) => this.row(d)) : [h("div", { class: "dl-empty" }, "No drawings on this instrument")];
    el.replaceChildren(head, ...rows);
  }

  private row(d: Drawing): HTMLElement {
    const hidden = isHiddenDrawing(d);
    const locked = isLocked(d);
    const iconBtn = (name: IconName, title: string, onClick: () => void, opts: { active?: boolean; disabled?: boolean } = {}) => {
      const b = button({ icon: name, title, disabled: opts.disabled, onClick });
      b.classList.toggle("active", Boolean(opts.active));
      return b;
    };
    const title = h("button", { type: "button", class: "dl-title", title: "Select" }, rowTitle(d));
    title.addEventListener("click", () => this.ctl.select(hidden ? undefined : d.id));
    return h(
      "div",
      { class: `dl-row${d.id === this.ctl.selectedId ? " selected" : ""}${hidden ? " is-hidden" : ""}` },
      h("span", { class: "dl-swatch", style: `background:${d.style.color}` }),
      icon(d.type, 16),
      title,
      iconBtn(hidden ? "eyeOff" : "eye", hidden ? "Show" : "Hide", () => void this.ctl.update(d.id, { style: { hidden: !hidden } }), { active: hidden }),
      iconBtn(locked ? "lockOpen" : "lock", locked ? "Unlock" : "Lock", () => void this.ctl.update(d.id, { style: { locked: !locked } }), { active: locked }),
      iconBtn("trash", locked ? "Unlock to delete" : "Delete", () => void this.ctl.remove(d.id), { disabled: locked }),
    );
  }
}
