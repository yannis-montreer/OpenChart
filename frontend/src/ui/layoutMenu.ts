/**
 * The layout menu in the top bar (SPEC section 11): save the chart as a named layout on
 * the server, load one, rename, delete, and pick the default one that loads on start.
 */

import { ApiError, layoutsApi, type Layout, type LayoutState, type LayoutSummary } from "../api";
import { button, closeMenu, h, icon, menuItem, showMenu } from "./dom";
import { toast } from "./toast";

export interface LayoutHooks {
  /** The chart as it is now. */
  state: () => LayoutState;
  /** Load a layout onto the chart. */
  apply: (layout: Layout) => void;
}

export class LayoutMenu {
  readonly button: HTMLButtonElement;
  private layouts: LayoutSummary[] = [];
  /** The layout the chart was loaded from or last saved as. */
  currentId: string | null = null;
  onCurrentChange: (id: string | null) => void = () => {};

  constructor(private hooks: LayoutHooks) {
    this.button = h("button", { class: "btn layout-btn", type: "button", "data-tip": "Layouts: save, load, default" });
    this.button.addEventListener("click", () => this.open());
    this.render();
  }

  async load(): Promise<LayoutSummary[]> {
    this.setList(await layoutsApi.list());
    return this.layouts;
  }

  setList(list: LayoutSummary[]): void {
    this.layouts = list;
    if (this.currentId && !list.some((l) => l.id === this.currentId)) this.setCurrent(null);
    this.render();
  }

  has(id: string | null): boolean {
    return id !== null && this.layouts.some((l) => l.id === id);
  }

  setCurrent(id: string | null): void {
    this.currentId = id;
    this.onCurrentChange(id);
    this.render();
  }

  private current(): LayoutSummary | undefined {
    return this.layouts.find((l) => l.id === this.currentId);
  }

  private render(): void {
    const name = this.current()?.name;
    this.button.replaceChildren(icon("layout", 16), h("span", {}, name ?? "Layout"), icon("chevron", 14));
    this.button.classList.toggle("unsaved", !name);
  }

  /** Save the chart into the current layout (or ask for a name). */
  async save(): Promise<void> {
    const cur = this.current();
    if (!cur) return this.open("save-as");
    try {
      const saved = await layoutsApi.update(cur.id, { layout: this.hooks.state() });
      toast({ title: "Layout saved", body: saved.name, icon: "save", timeout: 2500 });
    } catch (err) {
      toast({ title: "Could not save the layout", body: message(err), icon: "warn" });
    }
  }

  open(mode: "list" | "save-as" | "rename" = "list"): void {
    const box = h("div", { class: "layout-menu", style: "min-width:280px" });
    const fill = (m: typeof mode) => box.replaceChildren(...this.content(m, fill));
    fill(mode);
    if (!document.querySelector(".menu") || mode === "list") showMenu(this.button, box);
    else {
      closeMenu();
      showMenu(this.button, box);
    }
    (box.querySelector("input") as HTMLInputElement | null)?.focus();
  }

  private content(mode: "list" | "save-as" | "rename", refill: (m: "list" | "save-as" | "rename") => void): Node[] {
    const cur = this.current();
    if (mode !== "list") return [this.nameForm(mode, cur, () => refill("list"))];
    const items: Node[] = [
      h("div", { class: "menu-section" }, cur ? cur.name : "Not saved as a layout"),
      menuItem({ label: "Save", icon: "save", hint: cur ? "" : "asks for a name", onClick: () => void this.save() }),
      this.keepOpen(menuItem({ label: "Save as new layout…", icon: "plus" }), () => refill("save-as")),
    ];
    if (cur) items.push(this.keepOpen(menuItem({ label: "Rename…", icon: "pencil" }), () => refill("rename")));
    items.push(h("hr"), h("div", { class: "menu-section" }, "Layouts"));
    if (!this.layouts.length) items.push(h("p", { class: "al-note" }, "None saved yet."));
    for (const l of this.layouts) items.push(this.layoutItem(l, () => refill("list")));
    items.push(h("p", { class: "al-note" }, "The starred layout loads when OpenChart opens."));
    return items;
  }

  private keepOpen(item: HTMLButtonElement, then: () => void): HTMLButtonElement {
    item.addEventListener(
      "click",
      (ev) => {
        ev.stopImmediatePropagation();
        then();
      },
      { capture: true },
    );
    return item;
  }

  private layoutItem(l: LayoutSummary, refill: () => void): HTMLElement {
    const star = button({
      icon: "star",
      title: l.default ? "Loads on start (click to unset)" : "Load this layout on start",
      class: `star${l.default ? " on" : ""}`,
      onClick: async (ev) => {
        ev.stopPropagation();
        try {
          await layoutsApi.update(l.id, { default: !l.default });
          await this.load();
          refill();
        } catch (err) {
          toast({ title: "Could not change the default layout", body: message(err), icon: "warn" });
        }
      },
    });
    const del = button({
      icon: "trash",
      title: "Delete this layout",
      onClick: (ev) => {
        ev.stopPropagation();
        row.replaceWith(this.confirmDelete(l, refill));
      },
    });
    const row = menuItem({
      label: l.name,
      selected: l.id === this.currentId,
      extra: h("span", { class: "layout-item-actions" }, star, del),
      onClick: () => void this.loadLayout(l.id),
    });
    return row;
  }

  private confirmDelete(l: LayoutSummary, refill: () => void): HTMLElement {
    const yes = h("button", { class: "btn btn-danger", type: "button" }, "Delete");
    const no = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
    no.addEventListener("click", (ev) => {
      ev.stopPropagation();
      refill();
    });
    yes.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try {
        await layoutsApi.remove(l.id);
        await this.load();
      } catch (err) {
        toast({ title: "Could not delete the layout", body: message(err), icon: "warn" });
      }
      refill();
    });
    return h("div", { class: "layout-name-row" }, h("span", { class: "al-note", style: "flex:1;align-self:center" }, `Delete "${l.name}"?`), no, yes);
  }

  private nameForm(mode: "save-as" | "rename", cur: LayoutSummary | undefined, back: () => void): HTMLElement {
    const input = h("input", { class: "input", maxlength: "80", placeholder: "Layout name", spellcheck: "false" });
    input.value = mode === "rename" && cur ? cur.name : "";
    const go = h("button", { class: "btn btn-light", type: "button" }, mode === "rename" ? "Rename" : "Save");
    const note = h("p", { class: "al-note" });
    const submit = async () => {
      const name = input.value.trim();
      if (!name) return input.focus();
      go.setAttribute("disabled", "");
      try {
        if (mode === "rename" && cur) {
          await layoutsApi.update(cur.id, { name });
        } else {
          const made = await layoutsApi.create({ name, layout: this.hooks.state() });
          this.currentId = made.id;
          this.onCurrentChange(made.id);
          toast({ title: "Layout saved", body: made.name, icon: "save", timeout: 2500 });
        }
        await this.load();
        closeMenu();
      } catch (err) {
        note.textContent = message(err);
        go.removeAttribute("disabled");
      }
    };
    go.addEventListener("click", () => void submit());
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") void submit();
      if (ev.key === "Escape") back();
    });
    return h(
      "div",
      {},
      h("div", { class: "menu-section" }, mode === "rename" ? "Rename layout" : "Save as new layout"),
      h("div", { class: "layout-name-row" }, input, go),
      note,
    );
  }

  private async loadLayout(id: string): Promise<void> {
    try {
      const layout = await layoutsApi.get(id);
      this.setCurrent(id);
      this.hooks.apply(layout);
    } catch (err) {
      toast({ title: "Could not load the layout", body: message(err), icon: "warn" });
    }
  }
}

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}
